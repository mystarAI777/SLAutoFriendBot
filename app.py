# ==============================================================================
# もちこAI - v33.8.0 + pgvector RAGシステム + Gemini 2.5モデル更新
#
# 手動パッチガイド v2 - ニックネーム・応答崩壊・古タスク修正
# 
# ベース: v33.7.0 (全機能保持)
# 追加機能 (v33.8.0):
# 12. pgvector RAGシステム (HoloRAGクラス / PostgreSQL vectorストア)
# 13. Gemini text-embedding-004 によるセマンティック検索
# 14. GitHub Actions による自動ベクターDB構築 (build_holo_rag.yml)
# 15. Geminiモデル更新 (gemini-2.5-flash / gemini-2.5-flash-lite)
# 16. PostgreSQLシーケンス修正対象テーブル追加 (user_interest_logs等)
# ===
# ベース: v33.6.0 (全機能保持)
# 追加機能 (v33.7.0):
# セカンドライフ情報収集 (SecondLifeNews テーブル / 30日自動削除)
# アニメ自動検索 & 知識キャッシュ (AnimeInfoCache テーブル / 7日更新)
# ===
# ベース: v33.5.0 (全機能保持)
# 追加機能 (v33.6.0):
# 省エネ外部cronスケジューラ (GitHub Actions / 1日2回wake)
# /wake エンドポイント追加 (WAKE_API_KEY認証)
# TaskLog テーブル追加 (タスク実行履歴管理)
# ===
# ベース: v33.3.0 (全機能保持)
# 追加機能 (v33.4.0):
# 7. 友達プロフィール自動構築 (FriendProfile テーブル)
# 8. 興味ログ自動蓄積 (UserInterestLog テーブル / 90日自動削除)
# 9. 会話ごとのリアルタイム興味キーワード抽出
# 10. 友達の記憶を全プロンプトに注入
# 11. 友達向け能動的な話題振り生成
# ===
# ベース: v33.2.0 (全機能保持)
# 追加機能 (v33.3.0):
# 1. 配信情報の自動収集 (5chまとめ/Yahoo!リアルタイム検索から)
# 2. もちこの感想データベース (HolomemFeelings テーブル)
# 3. 配信ごとの反応記録 (StreamReactions テーブル / 1ヶ月自動削除)
# 4. 感情分析エンジン (もちこの性格に基づいた反応生成)
# 5. 記憶の蓄積と参照 (過去の感想を会話に反映)
# 6. データベース容量管理システム
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
import concurrent.futures as _cf  # ← これも先頭の import ブロックに追加
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
_CHAT_TIMEOUT_SECONDS = 18  # ✨ これを追加！ (Add this line)

# パーソナライズ設定
FRIEND_THRESHOLD = 5
ANALYSIS_INTERVAL = 5
TOPIC_SUGGESTION_INTERVAL = 10

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]
# ==========================================
# Groqで使用するモデルの優先順位 (2026年実務最適化版)
# ==========================================
GROQ_MODELS = [
    "llama-3.3-70b-versatile",        # メイン火力：汎用性・日本語能力・コードの総合力No.1
    "deepseek-r1-distill-llama-70b",  # 推論特化：複雑なロジックや長考が必要な時用
    "deepseek-r1-distill-qwen-32b",   # Qwen系エース：コードと論理推論のバランス型
    "qwen-2.5-32b",                   # 多言語ベース：Qwen独自の素直な応答が必要な時
    "llama-3.1-8b-instant"            # 高速・軽量：Botの日常会話や補助タスクの常用
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
    friend_profile: Optional[Dict] = None   # ★ v33.4.0追加: 友達プロフィール
    nickname: Optional[str] = None           # ★ 追加: ユーザー指定の呼び方

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

class GeminiModelManager:
    """Geminiモデルのフォールバック管理"""
    def __init__(self):
        self._lock = RLock()
        self._models = GEMINI_MODELS
        self._current_index = 0
        self._status = GeminiModelStatus()
        self._gemini_instances = {}
    
    def get_current_model(self) -> Optional[Any]:
        with self._lock:
            if self._status.is_limited and self._status.reset_time:
                if datetime.utcnow() >= self._status.reset_time:
                    logger.info(f"✅ Gemini制限解除: {self._status.current_model}")
                    self._status.is_limited = False
                    self._status.reset_time = None
            
            if self._status.is_limited:
                self._current_index = (self._current_index + 1) % len(self._models)
                self._status.current_model = self._models[self._current_index]
                self._status.is_limited = False
                logger.info(f"🔄 Geminiモデル切り替え: {self._status.current_model}")
            
            model_name = self._models[self._current_index]
            
            if model_name not in self._gemini_instances:
                try:
                    self._gemini_instances[model_name] = genai.GenerativeModel(model_name)
                    logger.info(f"🆕 Geminiモデル初期化: {model_name}")
                except Exception as e:
                    logger.error(f"❌ Gemini初期化失敗 ({model_name}): {e}")
                    return None
            
            return self._gemini_instances[model_name]
    
    def mark_limited(self, wait_seconds: int = 60):
        with self._lock:
            self._status.is_limited = True
            self._status.reset_time = datetime.utcnow() + timedelta(seconds=wait_seconds)
            logger.warning(f"⚠️ Gemini制限検知 ({self._status.current_model}): {wait_seconds}秒後にリトライ")
    
    def get_status_report(self) -> str:
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
# GitHub Actions から /wake を叩く際の認証キー
# Render の Environment Variables と GitHub の Secrets に同じ値を設定する
WAKE_API_KEY = get_secret('WAKE_API_KEY')

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
    nickname = Column(String(100), nullable=True)          # ★ 追加: 呼び方
    nickname_asked = Column(Boolean, default=False)        # ★ 追加: 呼び方確認済みフラグ

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

class TaskLog(Base):
    """
    バックグラウンドタスクの最終実行時刻を記録する。
    Renderのスリープ復帰時にキャッチアップ実行を判断するために使う。
    """
    __tablename__ = 'task_logs'
    task_name = Column(String(100), primary_key=True)
    last_run = Column(DateTime, default=datetime.utcnow)
    last_success = Column(DateTime, nullable=True)   # 最後に正常完了した時刻
    run_count = Column(Integer, default=0)           # 累計実行回数
    last_error = Column(Text, nullable=True)         # 最後のエラーメッセージ

# ==============================================================================
# ★ 追加 (v33.4.0): 友達プロフィール & 興味ログテーブル
# ==============================================================================
class FriendProfile(Base):
    """
    友達登録済みユーザーの詳細プロフィール。
    もちこが「この人のこと」を覚えておくための長期記憶。
    """
    __tablename__ = 'friend_profiles'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)

    # ホロライブ系の好み
    fav_holomem = Column(String(300), nullable=True)       # 好きなホロメン (カンマ区切り)
    fav_gen = Column(String(100), nullable=True)           # 好きな世代
    fav_stream_type = Column(String(200), nullable=True)   # 好きな配信ジャンル

    # 一般的な趣味・嗜好
    hobbies = Column(String(300), nullable=True)           # 趣味 (カンマ区切り)
    fav_games = Column(String(300), nullable=True)         # 好きなゲーム
    fav_music = Column(String(300), nullable=True)         # 好きな音楽
    fav_anime = Column(String(300), nullable=True)         # 好きなアニメ・漫画

    # 個人メモ (AIが会話から自動更新)
    memo = Column(Text, nullable=True)                     # もちこのメモ (自由形式・400文字)
    mood_tendency = Column(String(50), nullable=True)      # 普段のテンション (元気/落ち着き/etc)

    # 統計
    friend_since = Column(DateTime, default=datetime.utcnow)
    last_deep_talk = Column(DateTime, nullable=True)       # 最後に深い話をした日時
    total_friend_messages = Column(Integer, default=0)

    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class UserInterestLog(Base):
    """
    会話から自動抽出した興味・言及ログ。
    具体的なキーワードを蓄積して「この人が何を好きか」を精度高く把握する。
    古いエントリは自動削除 (90日)。
    """
    __tablename__ = 'user_interest_logs'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)  # holomem / game / anime / music / etc
    keyword = Column(String(100), nullable=False)              # 具体的なキーワード
    mention_count = Column(Integer, default=1)                 # 言及回数
    sentiment = Column(String(10), default='positive')         # positive / negative / neutral
    last_mentioned = Column(DateTime, default=datetime.utcnow, index=True)
    first_mentioned = Column(DateTime, default=datetime.utcnow)

# ==============================================================================
# ★ 追加 (v33.3.0): 配信反応 & もちこの想いテーブル
# ==============================================================================
class StreamReaction(Base):
    """配信への反応記録 (1ヶ月で自動削除)"""
    __tablename__ = 'stream_reactions'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), nullable=False, index=True)
    stream_title = Column(String(300), nullable=False)
    stream_date = Column(DateTime, nullable=False, index=True)
    mochiko_feeling = Column(String(300), nullable=False)
    emotion_tags = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class HolomemFeeling(Base):
    """ホロメンへのもちこの想い (要約版・容量制限)"""
    __tablename__ = 'holomem_feelings'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), unique=True, nullable=False, index=True)
    summary_feeling = Column(String(400), nullable=True)
    memorable_streams = Column(String(500), nullable=True)
    total_watch_count = Column(Integer, default=0)
    love_level = Column(Integer, default=50)
    last_emotion = Column(String(20), nullable=True)
    last_watched = Column(DateTime, nullable=True)
    last_summary_update = Column(DateTime, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ==============================================================================
# ★ 追加 (v33.7.0): セカンドライフニュース & アニメキャッシュテーブル
# ==============================================================================
class SecondLifeNews(Base):
    """
    セカンドライフの最新情報を蓄積するテーブル。
    公式ブログ・コミュニティニュース・マーケットトレンドなどを保存。
    30日で自動削除。
    """
    __tablename__ = 'secondlife_news'
    id = Column(Integer, primary_key=True)
    source = Column(String(100), nullable=False)    # 'official_blog' / 'community' / 'marketplace'
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=True)
    url = Column(String(1000), nullable=True)
    news_hash = Column(String(100), unique=True, index=True)
    category = Column(String(50), nullable=True)    # 'update' / 'event' / 'trend' / 'news'
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class AnimeInfoCache(Base):
    """
    アニメ作品情報のキャッシュ。
    会話中に知らないアニメが出てきた時にWeb検索した結果を保存し、
    次回の同じ作品への言及時は検索不要になる。7日で自動更新。
    """
    __tablename__ = 'anime_info_cache'
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False, unique=True, index=True)
    synopsis = Column(Text, nullable=True)
    genre = Column(String(200), nullable=True)
    status = Column(String(50), nullable=True)      # '放送中' / '完結' / '放送前'
    season = Column(String(50), nullable=True)      # '2025年秋' など
    raw_search_result = Column(Text, nullable=True) # 検索結果の生テキスト (最大500文字)
    cached_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_accessed = Column(DateTime, default=datetime.utcnow)

# ==============================================================================
# ★ 追加: 専門サイト検索結果キャッシュテーブル
# ==============================================================================
class SpecializedNews(Base):
    """
    専門サイト（Blender公式ドキュメント・CGニュース等）への
    スコープ限定検索から得た知見を蓄積するテーブル。
    ユーザーが質問するたびに最新情報が追加される自律学習型。
    SL・アニメは専用テーブルで管理するため除外。30日で自動削除。
    """
    __tablename__ = 'specialized_news'
    id = Column(Integer, primary_key=True)
    site_name = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=True)
    url = Column(String(1000), nullable=True)
    news_hash = Column(String(100), unique=True, index=True)
    query_keyword = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

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


def sanitize_response_for_sl(text: str) -> str:
    """
    SLチャット送信前のサニタイズ処理。
    1. パイプ文字 | を全角に置換（LSLデリミタ衝突防止）
    2. 同じ文字が15回以上連続する場合を圧縮（えええ...バグ対策）
    3. 連続する改行を最大2個に制限
    """
    if not text:
        return text
    # パイプを全角パイプに置換
    text = text.replace('|', '｜')
    # 同じ文字が15回以上連続 → 5回 + 省略記号に圧縮
    text = re.sub(r'(.)\1{14,}', lambda m: m.group(1) * 5 + '…', text)
    # 3回以上の連続改行を2回に制限
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text

def get_weather_forecast(location: str = "東京") -> str:
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
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != user_name: user.user_name = user_name
        if hasattr(user, 'is_friend'):
            if user.interaction_count >= FRIEND_THRESHOLD and not user.is_friend:
                user.is_friend = True
                logger.info(f"🎉 {user_name}さんが友達に認定されました！")
        else:
            user.is_friend = False
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
        session.add(user)
    
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

    # ★ v33.4.0: 友達プロフィールを読み込む
    friend_profile_data = None
    is_friend = getattr(user, 'is_friend', False)
    if is_friend:
        fp = session.query(FriendProfile).filter_by(user_uuid=user_uuid).first()
        if fp:
            friend_profile_data = {
                'fav_holomem': fp.fav_holomem,
                'fav_gen': fp.fav_gen,
                'fav_stream_type': fp.fav_stream_type,
                'hobbies': fp.hobbies,
                'fav_games': fp.fav_games,
                'fav_music': fp.fav_music,
                'fav_anime': fp.fav_anime,
                'memo': fp.memo,
                'mood_tendency': fp.mood_tendency,
                'total_friend_messages': fp.total_friend_messages,
            }
            # 友達メッセージ数を更新
            fp.total_friend_messages += 1
            fp.last_updated = datetime.utcnow()
        else:
            # 友達認定されたばかりの場合、プロフィールを新規作成
            fp = FriendProfile(user_uuid=user_uuid, user_name=user_name)
            session.add(fp)

    # ★ 追加: ニックネームを読み込む
    user_nickname = getattr(user, 'nickname', None)

    return UserData(
        uuid=user.user_uuid,
        name=user.user_name,
        interaction_count=user.interaction_count,
        is_friend=is_friend,
        favorite_topics=fav_topics,
        psychology=psych_data,
        friend_profile=friend_profile_data,
        nickname=user_nickname
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
    if info.get('recent_activity'):
         context += f"\n【{info['member_name']}に関する直近のX(Twitter)の様子・話題】\n{info['recent_activity']}\n"
    
    return context

# ==============================================================================
# Yahoo!リアルタイム検索連携
# ==============================================================================
def scrape_yahoo_realtime_for_member(member_name: str) -> str:
    """指定したメンバーのリアルタイム検索結果をテキストで返す"""
    try:
        query = f"{member_name} -RT"
        url = "https://search.yahoo.co.jp/realtime/search"
        params = {'p': query, 'ei': 'UTF-8', 'm': 'latency'}
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code != 200: return ""
        
        soup = BeautifulSoup(res.content, 'html.parser')
        texts = []
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
    """全ホロメンの最新状況をYahooから収集してDB更新"""
    logger.info("🐦 ホロメンSNS状況更新タスク開始")
    with get_db_session() as session:
        members = session.query(HolomemWiki).order_by(HolomemWiki.last_updated.asc()).limit(5).all()
        
        for m in members:
            logger.info(f"🔎 {m.member_name} の最新状況を収集中...")
            activities = scrape_yahoo_realtime_for_member(m.member_name)
            
            if activities:
                m.recent_activity = activities
                m.last_updated = datetime.utcnow()
                clear_holomem_cache(m.member_name)
            
            time.sleep(3)
            
    logger.info("✅ SNS状況更新完了")

# ==============================================================================
# ★ 追加 (v33.3.0): 配信情報収集システム
# ==============================================================================
def scrape_hololive_stream_info() -> List[Dict]:
    """
    ホロライブの配信情報をネットから収集
    - 5chまとめサイト
    - Yahoo!リアルタイム検索
    """
    logger.info("📺 配信情報収集開始...")
    stream_data = []
    
    try:
        matome_sites = [
            "https://vtubermatome.com/",
            "https://hololive-tsuushin.com/"
        ]
        
        for site in matome_sites:
            try:
                res = requests.get(site, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=10)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content, 'html.parser')
                    articles = soup.select('article, .post')[:10]
                    for art in articles:
                        title_elem = art.find(['h1', 'h2', 'h3'])
                        if title_elem:
                            title = clean_text(title_elem.text)
                            if any(kw in title for kw in ['配信', '切り抜き', 'ライブ', '歌枠', 'コラボ']):
                                stream_data.append({
                                    'title': title,
                                    'source': 'matome',
                                    'url': art.find('a').get('href') if art.find('a') else '',
                                    'date': datetime.utcnow()
                                })
                time.sleep(2)
            except Exception as e:
                logger.warning(f"まとめサイト収集エラー ({site}): {e}")
    except Exception as e:
        logger.error(f"配信情報収集エラー: {e}")
    
    return stream_data

def analyze_stream_reactions(member_name: str, stream_title: str) -> Dict:
    """配信への反応を分析 (Yahoo!リアルタイム検索から)"""
    reactions = {
        'positive_keywords': [],
        'highlight_moments': [],
        'fan_comments': []
    }
    
    try:
        query = f"{member_name} {stream_title[:20]}"
        url = "https://search.yahoo.co.jp/realtime/search"
        params = {'p': query, 'ei': 'UTF-8'}
        
        res = requests.get(url, params=params, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content, 'html.parser')
            tweets = soup.select('.cnt.cf')[:10]
            for tweet in tweets:
                txt = tweet.select_one('.kw')
                if txt:
                    content = clean_text(txt.text)
                    if any(kw in content for kw in ['神', '最高', '面白', '笑った', '感動', '泣いた', 'エモい']):
                        reactions['positive_keywords'].append(content[:50])
                    if any(kw in content for kw in ['シーン', '場面', 'ここ', 'タイムスタンプ', '爆笑']):
                        reactions['highlight_moments'].append(content[:50])
    
    except Exception as e:
        logger.error(f"反応分析エラー: {e}")
    
    return reactions

# ==============================================================================
# ★ 追加 (v33.3.0): もちこの感想生成エンジン
# ==============================================================================
def generate_mochiko_reaction(member_name: str, stream_title: str, reactions: Dict) -> Dict:
    """ネットの反応からもちこの感想を生成 (詳細版: 300文字)"""
    emotion_tags = []
    if any('笑' in kw or '爆笑' in kw for kw in reactions.get('positive_keywords', [])):
        emotion_tags.append('爆笑')
    if any('感動' in kw or '泣' in kw for kw in reactions.get('positive_keywords', [])):
        emotion_tags.append('感動')
    if any('神' in kw or '最高' in kw for kw in reactions.get('positive_keywords', [])):
        emotion_tags.append('興奮')
    
    prompt = f"""あなたは「もちこ」というホロライブの熱狂的なファンです。

【配信情報】
- ホロメン: {member_name}
- タイトル: {stream_title}
- ネットの反応: {', '.join(reactions.get('positive_keywords', [])[:5])}

【あなたの役割】
この配信を見た「もちこ」として、具体的で感情豊かな感想を300文字以内で書いてください。

【もちこの口調】
- 一人称: 「あてぃし」
- 語尾: 「〜じゃん」「〜て感じ」「まじ」「超」「やばい」
- 感情豊かで熱量高め、具体的な描写を入れる

【良い例 (濃い)】
「まじ神回！{member_name}のあのリアクション爆笑すぎてお腹痛いww しかもトークのテンポ感が最高で、あの展開まじ予想外だったし。ファンサも神だったわ〜」

【出力形式】
感想だけを出力してください (説明不要)
"""
    
    try:
        model = gemini_model_manager.get_current_model()
        if model:
            response = model.generate_content(prompt, generation_config={"temperature": 0.9, "max_output_tokens": 1000})
            if hasattr(response, 'candidates') and response.candidates:
                feeling = response.candidates[0].content.parts[0].text.strip()
                return {
                    'feeling': feeling[:300],
                    'emotion_tags': ','.join(emotion_tags),
                    'favorite_part': reactions.get('highlight_moments', [''])[0] if reactions.get('highlight_moments') else None
                }
    except Exception as e:
        logger.error(f"感想生成エラー: {e}")
    
    # フォールバック: 詳細テンプレート
    templates = [
        f"{member_name}の配信まじ最高だった！特に{stream_title[:20]}...の展開、予想外すぎて爆笑したわ。トークのテンポ感も神だし、リスナーとのやり取りも面白すぎる。",
        f"今日の{member_name}、神回すぎてやばい。{stream_title[:20]}...って内容だったんだけど、あのリアクション最高すぎて何回も見返してる。まじ推せるわ〜",
        f"{member_name}のこの配信、何回でも見れるわ〜。{stream_title[:20]}...のシーン、まじ感動したし泣きそうになった。あの距離感と優しさがほんと好き。"
    ]
    
    return {
        'feeling': random.choice(templates)[:300],
        'emotion_tags': ','.join(emotion_tags),
        'favorite_part': None
    }

# ==============================================================================
# ★ 追加 (v33.3.0): データベース容量管理システム
# ==============================================================================
def cleanup_old_stream_reactions():
    """1ヶ月以上前の配信反応を自動削除"""
    logger.info("🗑️ 古い配信反応の削除開始...")
    
    try:
        cutoff_date = datetime.utcnow() - timedelta(days=30)
        with get_db_session() as session:
            old_count = session.query(StreamReaction).filter(
                StreamReaction.created_at < cutoff_date
            ).count()
            
            if old_count > 0:
                session.query(StreamReaction).filter(
                    StreamReaction.created_at < cutoff_date
                ).delete()
                logger.info(f"✅ {old_count}件の古い反応を削除しました")
            else:
                logger.info("✅ 削除対象なし")
                
    except Exception as e:
        logger.error(f"❌ クリーンアップエラー: {e}")

def generate_feeling_summary(member_name: str, reactions: List) -> Optional[Dict]:
    """複数の感想を1つに要約 (詳細版: 解像度を保つ)"""
    if not reactions:
        return None
    
    feelings_text = "\n".join([
        f"- 「{r.stream_title[:40]}」: {r.mochiko_feeling}" 
        for r in reactions[:10]
    ])
    
    prompt = f"""あなたは「もちこ」というホロライブの熱狂的なファンです。

【配信情報】
ホロメン: {member_name}
過去1ヶ月の感想:
{feelings_text}

【要約タスク】
上記の感想を400文字以内で要約してください。

【重要な要件】
1. 「もちこ」の口調 (ギャル語) を維持
2. 400文字以内
3. このホロメンの「どこが好きか」「どんな配信が好きか」「最近の印象」を具体的に
4. 個性や魅力が伝わる内容に (解像度を高く保つ)
5. 感情豊かに、熱量を持って表現

【出力形式】
要約だけを出力してください (前置き不要)
"""
    
    try:
        model = gemini_model_manager.get_current_model()
        if model:
            response = model.generate_content(
                prompt, 
                generation_config={"temperature": 0.8, "max_output_tokens": 1000}
            )
            if hasattr(response, 'candidates') and response.candidates:
                summary = response.candidates[0].content.parts[0].text.strip()
                memorable = []
                for r in reactions[:5]:
                    if any(tag in (r.emotion_tags or '') for tag in ['興奮', '感動']):
                        memorable.append(r.stream_title[:60])
                return {
                    'summary': summary[:400],
                    'memorable': '、'.join(memorable)[:500]
                }
    except Exception as e:
        logger.error(f"要約生成エラー: {e}")
    
    return None

def summarize_member_feelings():
    """各ホロメンへの想いを定期的に要約 (週1回実行)"""
    logger.info("📝 想いの要約処理開始...")
    
    try:
        with get_db_session() as session:
            feelings = session.query(HolomemFeeling).all()
            
            for feeling in feelings:
                if feeling.last_summary_update:
                    days_since_update = (datetime.utcnow() - feeling.last_summary_update).days
                    if days_since_update < 7:
                        continue
                
                one_month_ago = datetime.utcnow() - timedelta(days=30)
                recent_reactions = session.query(StreamReaction).filter(
                    StreamReaction.member_name == feeling.member_name,
                    StreamReaction.created_at >= one_month_ago
                ).order_by(StreamReaction.created_at.desc()).limit(15).all()
                
                if len(recent_reactions) < 3:
                    continue
                
                summary = generate_feeling_summary(feeling.member_name, recent_reactions)
                
                if summary:
                    feeling.summary_feeling = summary['summary'][:400]
                    feeling.memorable_streams = summary['memorable'][:500]
                    feeling.last_summary_update = datetime.utcnow()
                    logger.info(f"✅ {feeling.member_name}の想いを要約 (解像度: 高)")
                
                time.sleep(2)
                
    except Exception as e:
        logger.error(f"❌ 要約処理エラー: {e}")

def get_database_size_stats() -> Dict:
    """データベースの容量統計を取得"""
    stats = {}
    try:
        with get_db_session() as session:
            stats['stream_reactions'] = session.query(StreamReaction).count()
            stats['holomem_feelings'] = session.query(HolomemFeeling).count()
            
            oldest = session.query(StreamReaction).order_by(StreamReaction.created_at.asc()).first()
            newest = session.query(StreamReaction).order_by(StreamReaction.created_at.desc()).first()
            
            if oldest:
                stats['oldest_reaction'] = oldest.created_at.isoformat()
            if newest:
                stats['newest_reaction'] = newest.created_at.isoformat()
    except Exception as e:
        logger.error(f"統計取得エラー: {e}")
    
    return stats

def process_daily_streams():
    """1日1回実行: 配信情報を収集してもちこの感想を記録 (容量制限: 1日最大5件)"""
    logger.info("🎬 日次配信処理開始...")
    
    streams = scrape_hololive_stream_info()
    processed_count = 0
    max_daily_streams = 5
    
    with get_db_session() as session:
        for stream_info in streams[:10]:
            if processed_count >= max_daily_streams:
                break
            
            try:
                title = stream_info['title']
                detected_member = holomem_manager.detect_in_message(title)
                if not detected_member:
                    continue
                
                logger.info(f"📺 処理中: {detected_member} - {title[:30]}...")
                
                reactions = analyze_stream_reactions(detected_member, title)
                mochiko_reaction = generate_mochiko_reaction(detected_member, title, reactions)
                
                existing = session.query(StreamReaction).filter_by(
                    member_name=detected_member,
                    stream_title=title[:300]
                ).first()
                
                if not existing:
                    new_reaction = StreamReaction(
                        member_name=detected_member,
                        stream_title=title[:300],
                        stream_date=stream_info['date'],
                        mochiko_feeling=mochiko_reaction['feeling'][:300],
                        emotion_tags=mochiko_reaction['emotion_tags'][:50]
                    )
                    session.add(new_reaction)
                    processed_count += 1
                    
                    feeling = session.query(HolomemFeeling).filter_by(member_name=detected_member).first()
                    if not feeling:
                        feeling = HolomemFeeling(member_name=detected_member)
                        session.add(feeling)
                    
                    feeling.total_watch_count += 1
                    feeling.last_watched = datetime.utcnow()
                    feeling.last_emotion = mochiko_reaction['emotion_tags'].split(',')[0] if mochiko_reaction['emotion_tags'] else None
                    
                    if '興奮' in (mochiko_reaction['emotion_tags'] or ''):
                        feeling.love_level = min(100, feeling.love_level + 5)
                    elif '感動' in (mochiko_reaction['emotion_tags'] or ''):
                        feeling.love_level = min(100, feeling.love_level + 3)
                    
                    logger.info(f"✅ 感想記録: {mochiko_reaction['feeling'][:50]}...")
                
                time.sleep(3)
                
            except Exception as e:
                logger.error(f"配信処理エラー: {e}")
    
    logger.info(f"✅ 日次配信処理完了 ({processed_count}件処理)")
    cleanup_old_stream_reactions()

# ==============================================================================
# ★ 追加 (v33.3.0): もちこの記憶取得
# ==============================================================================
def get_mochiko_memory_context(member_name: str) -> str:
    """もちこの記憶 (要約版) をコンテキストとして取得"""
    context = ""
    
    try:
        with get_db_session() as session:
            feeling = session.query(HolomemFeeling).filter_by(member_name=member_name).first()
            if feeling:
                context += f"\n【もちこの{member_name}への想い】\n"
                if feeling.summary_feeling:
                    context += f"{feeling.summary_feeling}\n"
                context += f"- 推し度: {feeling.love_level}/100 (視聴{feeling.total_watch_count}回)\n"
                if feeling.memorable_streams:
                    context += f"- 印象的だった配信: {feeling.memorable_streams}\n"
            
            one_month_ago = datetime.utcnow() - timedelta(days=30)
            recent_reactions = session.query(StreamReaction).filter(
                StreamReaction.member_name == member_name,
                StreamReaction.created_at >= one_month_ago
            ).order_by(StreamReaction.created_at.desc()).limit(3).all()
            
            if recent_reactions:
                context += f"\n【最近見た{member_name}の配信】\n"
                for r in recent_reactions:
                    context += f"- 「{r.stream_title[:30]}...」→ {r.mochiko_feeling[:50]}\n"
    
    except Exception as e:
        logger.error(f"記憶取得エラー: {e}")
    
    return context

# ==============================================================================
# ホロメンスクレイピング & DB更新
# ==============================================================================
def scrape_hololive_wiki() -> List[Dict]:
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

# ==============================================================================
# ★ 追加 (v33.7.0): セカンドライフ情報収集
# ==============================================================================

SL_SOURCES = [
    # 公式ブログ RSS
    {
        'name': 'official_blog',
        'type': 'rss',
        'url': 'https://community.secondlife.com/blogs.xml/',
        'category': 'news',
    },
    # SLuniverseニュース RSS
    {
        'name': 'sluniverse',
        'type': 'rss',
        'url': 'https://www.sluniverse.com/php/vb/external.php?type=RSS2',
        'category': 'community',
    },
    # 公式Twitter/Xの代わりにGoogle News RSS (Second Life)
    {
        'name': 'google_news_sl',
        'type': 'google_news_rss',
        'query': 'Second Life virtual world',
        'category': 'news',
    },
    # Google News RSS (日本語SL情報)
    {
        'name': 'google_news_sl_jp',
        'type': 'google_news_rss',
        'query': 'セカンドライフ SL',
        'category': 'news',
    },
]

def _make_news_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def fetch_sl_rss(source_config: Dict) -> List[Dict]:
    """RSS/Atomフィードを取得してリストで返す"""
    results = []
    try:
        headers = {'User-Agent': random.choice(USER_AGENTS), 'Accept': 'application/rss+xml,application/xml,text/xml'}
        res = requests.get(source_config['url'], headers=headers, timeout=15)
        if res.status_code != 200:
            return []
        soup = BeautifulSoup(res.content, 'xml')
        items = soup.find_all('item') or soup.find_all('entry')
        for item in items[:8]:
            title_tag = item.find('title')
            desc_tag = item.find('description') or item.find('summary') or item.find('content')
            link_tag = item.find('link')
            if not title_tag:
                continue
            title = clean_text(title_tag.get_text())[:500]
            content = clean_text(desc_tag.get_text())[:600] if desc_tag else ''
            url = ''
            if link_tag:
                url = link_tag.get('href') or link_tag.get_text() or ''
            if title:
                results.append({
                    'source': source_config['name'],
                    'category': source_config.get('category', 'news'),
                    'title': title,
                    'content': content,
                    'url': url,
                })
    except Exception as e:
        logger.warning(f"SL RSS取得失敗 ({source_config['name']}): {e}")
    return results

def fetch_sl_google_news(source_config: Dict) -> List[Dict]:
    """Google News RSSでSL関連ニュースを取得"""
    results = []
    try:
        query = source_config['query']
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=ja&gl=JP&ceid=JP:ja"
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return []
        soup = BeautifulSoup(res.content, 'xml')
        for item in soup.find_all('item')[:5]:
            title = clean_text(item.title.get_text()) if item.title else ''
            link = item.link.get_text() if item.link else ''
            pub = item.pubDate.get_text() if item.pubDate else ''
            if title:
                results.append({
                    'source': source_config['name'],
                    'category': source_config.get('category', 'news'),
                    'title': title,
                    'content': f"({pub})" if pub else '',
                    'url': link,
                })
    except Exception as e:
        logger.warning(f"SL Google News取得失敗: {e}")
    return results

def fetch_sl_marketplace_trends() -> List[Dict]:
    """
    SL Marketplace の新着・人気アイテムをスクレイピング。
    トレンドとして保存する。
    """
    results = []
    try:
        url = "https://marketplace.secondlife.com/products/search?search[category_id]=0&search[sort]=relevance&search[per_page]=20"
        headers = {'User-Agent': random.choice(USER_AGENTS), 'Accept-Language': 'en-US,en;q=0.9'}
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return []
        soup = BeautifulSoup(res.content, 'html.parser')
        items = soup.select('.product-listing') or soup.select('[data-product-id]')
        seen_titles = set()
        for item in items[:10]:
            name_elem = item.select_one('.product-title, .item-name, h4 a, h3 a')
            price_elem = item.select_one('.price, .product-price')
            if not name_elem:
                continue
            name = clean_text(name_elem.get_text())[:200]
            price = clean_text(price_elem.get_text()) if price_elem else ''
            link = name_elem.get('href', '')
            if link and not link.startswith('http'):
                link = 'https://marketplace.secondlife.com' + link
            if name and name not in seen_titles:
                seen_titles.add(name)
                results.append({
                    'source': 'marketplace',
                    'category': 'trend',
                    'title': f"【マーケット】{name}",
                    'content': f"価格: {price}" if price else '',
                    'url': link,
                })
    except Exception as e:
        logger.warning(f"SL Marketplace取得失敗: {e}")
    return results

def fetch_all_sl_news() -> int:
    """
    全SLソースから情報を収集してDBに保存。
    重複はnews_hashで除外。返り値は新規保存件数。
    """
    logger.info("🌐 セカンドライフ情報収集開始...")
    all_items: List[Dict] = []

    for source in SL_SOURCES:
        if source['type'] == 'rss':
            all_items.extend(fetch_sl_rss(source))
        elif source['type'] == 'google_news_rss':
            all_items.extend(fetch_sl_google_news(source))
        time.sleep(0.5)  # 礼儀的なウェイト

    # Marketplace トレンド
    all_items.extend(fetch_sl_marketplace_trends())

    new_count = 0
    with get_db_session() as session:
        for item in all_items:
            h = _make_news_hash(item['title'])
            if session.query(SecondLifeNews).filter_by(news_hash=h).first():
                continue
            session.add(SecondLifeNews(
                source=item['source'],
                title=item['title'],
                content=item.get('content', ''),
                url=item.get('url', ''),
                news_hash=h,
                category=item.get('category', 'news'),
                created_at=datetime.utcnow(),
            ))
            new_count += 1

        # 30日以上前のデータを削除
        cutoff = datetime.utcnow() - timedelta(days=30)
        deleted = session.query(SecondLifeNews).filter(SecondLifeNews.created_at < cutoff).delete()
        if deleted:
            logger.info(f"🗑️ 古いSLニュース {deleted}件を削除")

    logger.info(f"✅ SL情報収集完了: 新規{new_count}件")
    return new_count

def get_sl_news_context(limit: int = 5) -> str:
    """
    最新のSLニュース・トレンドをプロンプト注入用テキストとして返す。
    """
    try:
        with get_db_session() as session:
            items = session.query(SecondLifeNews).order_by(
                SecondLifeNews.created_at.desc()
            ).limit(limit).all()
            if not items:
                return ''
            lines = []
            for it in items:
                cat_label = {'update': '🔧アップデート', 'event': '🎉イベント', 'trend': '🔥トレンド', 'news': '📰ニュース', 'community': '💬コミュニティ'}.get(it.category, '📰')
                line = f"{cat_label}【{it.source}】{it.title}"
                if it.content and len(it.content) > 5:
                    line += f" — {it.content[:120]}"
                lines.append(line)
            return "\n【セカンドライフ最新情報】\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"SLニュースコンテキスト取得エラー: {e}")
        return ''

# セカンドライフ関連キーワード検出
SL_KEYWORDS = [
    'セカンドライフ', 'second life', 'secondlife', 'SL', 'sl', 'インワールド', 'リンデン',
    'Linden', 'L$', 'アバター', 'リージョン', 'sim', 'SIM', 'メッシュ', 'ガチャ',
    'グループ', 'テレポート', 'TP', 'スキン', 'AO', 'ボディ', 'Maitreya', 'Belleza',
    'CATWA', 'BOM', 'ブレント', 'Firestorm', 'ビューア', 'マーケット', 'marketplace',
    'ロールプレイ', 'RP', 'フルパーミ', '全perm', 'ガレージセール', 'ハントイベント'
]

def is_sl_topic(message: str) -> bool:
    """メッセージがセカンドライフに関する発言かどうか判定"""
    msg_lower = message.lower()
    return any(kw.lower() in msg_lower for kw in SL_KEYWORDS)


# ==============================================================================
# ★ 追加: 専門サイト検索スコープ定義 & 検索ロジック
# ==============================================================================

SPECIALIZED_SITES: Dict[str, Dict] = {
    "blender": {
        "name": "Blender公式ドキュメント",
        "base_url": "https://docs.blender.org/manual/ja/latest/",
        "keywords": ["blender", "ブレンダー", "ブレンド", "blenderで", "blenderの"],
        "prompt_role": "Blender 3DCGソフトウェアの専門家",
        "category": "3dcg"
    },
    "cg_news": {
        "name": "ModelingHappy (CGニュース)",
        "base_url": "https://modelinghappy.com/",
        "keywords": ["cgニュース", "モデリングハッピー", "cg情報", "3dニュース", "cg業界"],
        "prompt_role": "CG・3DCG業界のニュース専門家",
        "category": "cg"
    },
    "science": {
        "name": "ナゾロジー (脳科学・心理学)",
        "base_url": "https://nazology.kusuguru.co.jp/",
        "keywords": ["脳科学", "心理学", "ナゾロジー", "認知科学", "神経科学", "行動経済学", "認知バイアス"],
        "prompt_role": "脳科学・心理学の専門家",
        "category": "science"
    },
}


def detect_specialized_topic(message: str) -> Optional[Dict]:
    """
    メッセージから専門サイト検索が必要なトピックを検出する。
    SL・アニメ系は既存システムが担当するため除外。
    一致したサイト情報(Dict)を返す。該当なしはNone。
    """
    msg_lower = message.lower()
    for key, site in SPECIALIZED_SITES.items():
        if any(kw.lower() in msg_lower for kw in site["keywords"]):
            return site
    return None


def save_to_specialized_news(
    site_name: str,
    title: str,
    content: str,
    url: str = "",
    query_keyword: str = ""
):
    """
    専門サイト検索結果をspecialized_newsテーブルに保存する。
    重複はnews_hashで除外。
    """
    try:
        news_hash = hashlib.md5(
            f"{site_name}:{title}:{content[:100]}".encode('utf-8')
        ).hexdigest()
        with get_db_session() as session:
            if not session.query(SpecializedNews).filter_by(news_hash=news_hash).first():
                session.add(SpecializedNews(
                    site_name=site_name,
                    title=title[:500],
                    content=content[:2000],
                    url=url[:1000] if url else "",
                    news_hash=news_hash,
                    query_keyword=query_keyword[:200] if query_keyword else "",
                    created_at=datetime.utcnow()
                ))
                logger.info(f"✅ specialized_news 保存: {site_name} - {title[:40]}")
    except Exception as e:
        logger.error(f"specialized_news 保存エラー: {e}")


def execute_specialized_site_search(query: str, site_info: Dict) -> str:
    """
    指定された専門サイト内のみをsite:演算子で検索し、専門家として回答する。
    検索結果はspecialized_newsテーブルに自動蓄積（次回以降のRAGに活用）。
    フォールバック: site:検索失敗時はサイト名込みの一般検索に切り替え。
    """
    target_site = site_info['base_url']
    search_query = f"site:{target_site} {query}"
    logger.info(f"🔍 専門サイト検索: {search_query[:80]}")

    results = scrape_major_search_engines(search_query, 4)

    if not results:
        fallback_query = f"{site_info['name']} {query}"
        logger.info(f"⚡ フォールバック検索: {fallback_query[:60]}")
        results = scrape_major_search_engines(fallback_query, 3)

    if not results:
        return (
            f"ごめん、{site_info['name']}の中を調べてみたけど、"
            f"直接の回答は見つからなかったじゃん…"
            f"公式サイト({target_site})を直接見てみてね！"
        )

    for r in results:
        save_to_specialized_news(
            site_name=site_info['name'],
            title=r.get('title', ''),
            content=r.get('snippet', ''),
            url=r.get('url', ''),
            query_keyword=query
        )

    results_text = "\n".join([
        f"{i+1}. {r['title']}: {r['snippet']}"
        for i, r in enumerate(results)
    ])

    prompt = f"""あなたは{site_info['name']}に精通した{site_info['prompt_role']}です。
以下の検索結果（{site_info['name']}内の情報）のみをソースとして、
ユーザーの質問「{query}」に回答してください。

【回答ルール】
- もちこの口調を維持（一人称: あてぃし、語尾: じゃん/だね/て感じ）
- {site_info['prompt_role']}として正確に、でも分かりやすく説明する
- 検索結果にない情報は「ドキュメントには記載がないじゃん」と正直に答える
- 300〜400文字以内に凝縮する

【検索結果】
{results_text}
"""

    response = None
    model = gemini_model_manager.get_current_model()
    if model:
        try:
            resp = model.generate_content(
                prompt,
                generation_config={"temperature": 0.5, "max_output_tokens": 1000}
            )
            if hasattr(resp, 'candidates') and resp.candidates:
                response = resp.candidates[0].content.parts[0].text.strip()
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "quota" in error_str.lower():
                retry_match = re.search(r'retry in (\d+(?:\.\d+)?)s', error_str)
                wait_seconds = int(float(retry_match.group(1))) + 5 if retry_match else 60
                gemini_model_manager.mark_limited(wait_seconds)
            logger.warning(f"専門検索Geminiエラー: {e}")

    if not response and groq_client:
        response = call_groq(prompt, query, [], 800)

    if not response:
        response = (
            f"{site_info['name']}で調べた結果じゃん！\n"
            f"{results_text[:300]}"
        )

    return response


def get_specialized_news_context(message: str, limit: int = 3) -> str:
    """
    specialized_newsテーブルからメッセージに関連する情報を取得し、
    プロンプト注入用テキストとして返す。
    キーワードマッチングで関連度を判断。
    """
    try:
        with get_db_session() as session:
            items = session.query(SpecializedNews).order_by(
                SpecializedNews.created_at.desc()
            ).limit(limit * 5).all()

            if not items:
                return ''

            query_words = [w for w in message.lower().split() if len(w) >= 2]
            scored: List[tuple] = []
            for item in items:
                kw = (item.query_keyword or '').lower()
                title_lower = (item.title or '').lower()
                content_lower = (item.content or '').lower()
                score = sum(
                    1 for w in query_words
                    if w in kw or w in title_lower or w in content_lower
                )
                if score > 0:
                    scored.append((score, item))

            scored.sort(key=lambda x: x[0], reverse=True)
            relevant = [item for _, item in scored[:limit]]

            if not relevant:
                return ''

            lines = []
            for it in relevant:
                line = f"【{it.site_name}】{it.title}"
                if it.content and len(it.content) > 5:
                    line += f"\n{it.content[:250]}"
                lines.append(line)

            return "\n\n【専門サイト検索キャッシュ】\n" + "\n\n".join(lines)

    except Exception as e:
        logger.error(f"specialized_news コンテキスト取得エラー: {e}")
        return ''


def cleanup_old_specialized_news():
    """30日以上前のspecialized_newsを削除"""
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        with get_db_session() as session:
            deleted = session.query(SpecializedNews).filter(
                SpecializedNews.created_at < cutoff
            ).delete()
            if deleted:
                logger.info(f"🗑️ 古い専門ニュース {deleted}件を削除")
    except Exception as e:
        logger.error(f"specialized_news クリーンアップエラー: {e}")



# ==============================================================================
# ★ 追加 (v33.7.0): アニメ自動検索 & 知識キャッシュ
# ==============================================================================

# 既知アニメ (もちこが事前に知っている主要作品。これにないものは自動検索)
KNOWN_ANIME_TITLES = {
    'ワンピース', '進撃の巨人', '鬼滅の刃', '呪術廻戦', '推しの子', 'チェンソーマン',
    '転スラ', 'リゼロ', 'ダンまち', 'ブルーロック', '葬送のフリーレン', 'ダンダダン',
    '薬屋のひとりごと', '異世界転生', 'ナルト', 'NARUTO', 'ドラゴンボール', 'ハンターハンター',
    '僕のヒーローアカデミア', 'ヒロアカ', '東京リベンジャーズ', 'SPY×FAMILY', 'スパイファミリー',
    'スラムダンク', '銀魂', '少年ジャンプ', '進撃', 'エヴァ', 'コードギアス',
    'BLEACH', 'ブリーチ', 'FAIRY TAIL', 'フェアリーテイル', 'ソードアートオンライン', 'SAO',
    'まどマギ', '魔法少女まどか', 'リコリコ', 'ぼっち・ざ・ろっく', '青のオーケストラ',
    'スキップとローファー', '浜辺美波', 'かげきしょうじょ',
}

# アニメっぽい発言を検出する正規表現パターン
ANIME_MENTION_PATTERN = re.compile(
    r'(?:見てる|観てる|見た|観た|ハマってる|好き|おすすめ|面白い|最高|神アニメ|神作画|作品|アニメ化|原作|漫画|マンガ|manga)'
    r'|(?:キャラ|主人公|ヒロイン|声優|OPが|EDが|第[\d一二三四五六七八九十百]+話|最終回|最新話)',
    re.IGNORECASE
)

def extract_anime_titles_from_message(message: str) -> List[str]:
    """
    メッセージからアニメ作品名と思われる単語を抽出する。
    既知リストにないもの (= もちこが知らない可能性がある) を返す。
    """
    candidates = []

    # カタカナ or 漢字2文字以上で構成される名詞的なトークンを候補とする
    # 「○○って知ってる?」「○○見てる」などのパターンから抽出
    patterns = [
        r'「([^」]{2,20})」',           # 鉤括弧内
        r'『([^』]{2,20})』',           # 二重鉤括弧内
        r'([ァ-ヶー]{3,20})(?:が|を|は|って|の)',   # カタカナ語
        r'([一-龥ぁ-んァ-ヶa-zA-Z]{2,15})(?:って|は|が)(?:面白|最高|神|おすすめ|好き)',
    ]

    for pat in patterns:
        for m in re.finditer(pat, message):
            word = m.group(1).strip()
            if word and word not in KNOWN_ANIME_TITLES and len(word) >= 2:
                # ホロメン名や一般語は除外
                if not any(kw in word for kw in ['ライブ','配信','ゲーム','スバル','みこ','ぺこ']):
                    candidates.append(word)

    return list(set(candidates))

def get_anime_info_from_cache_or_search(title: str) -> Optional[str]:
    """
    アニメ作品情報をキャッシュから取得、なければWeb検索して保存。
    返り値はプロンプト注入用テキスト (Noneなら情報なし)。
    """
    try:
        with get_db_session() as session:
            # キャッシュチェック (7日以内)
            cutoff = datetime.utcnow() - timedelta(days=7)
            cached = session.query(AnimeInfoCache).filter(
                AnimeInfoCache.title == title,
                AnimeInfoCache.cached_at >= cutoff
            ).first()

            if cached:
                cached.last_accessed = datetime.utcnow()
                logger.info(f"📦 アニメキャッシュHIT: {title}")
                return cached.raw_search_result

            # キャッシュなし → Web検索
            logger.info(f"🔍 アニメ情報検索: {title}")
            results = scrape_major_search_engines(f"{title} アニメ あらすじ 概要", 3)

            if not results:
                return None

            raw = "\n".join([f"{r['title']}: {r['snippet']}" for r in results])[:500]

            # キャッシュに保存
            existing = session.query(AnimeInfoCache).filter_by(title=title).first()
            if existing:
                existing.raw_search_result = raw
                existing.cached_at = datetime.utcnow()
                existing.last_accessed = datetime.utcnow()
            else:
                session.add(AnimeInfoCache(
                    title=title,
                    raw_search_result=raw,
                    cached_at=datetime.utcnow(),
                    last_accessed=datetime.utcnow(),
                ))

            return raw

    except Exception as e:
        logger.error(f"アニメ情報取得エラー ({title}): {e}")
        return None

def _get_anime_from_cache_only(title: str) -> Optional[str]:
    """
    DBキャッシュのみを参照してアニメ情報を返す。
    キャッシュMISSの場合は None を返し、Webアクセスは行わない。
    """
    try:
        cutoff = datetime.utcnow() - timedelta(days=7)
        with get_db_session() as session:
            cached = session.query(AnimeInfoCache).filter(
                AnimeInfoCache.title == title,
                AnimeInfoCache.cached_at >= cutoff
            ).first()
            if cached:
                cached.last_accessed = datetime.utcnow()
                return cached.raw_search_result
    except Exception as e:
        logger.error(f"アニメキャッシュ参照エラー ({title}): {e}")
    return None
    
def build_anime_context(message: str) -> str:
    """
    メッセージにアニメっぽい発言があれば、キャッシュ済み作品情報を返す。
    キャッシュMISS時はバックグラウンドで取得してスケジュール (次回HIT)。
    → 同期Webスクレイピングを一切行わないのでレスポンスをブロックしない。
    """
    # アニメ系の発言かどうか確認
    has_anime_keyword = any(kw in message for kw in ['アニメ', '漫画', 'マンガ', 'manga'])
    has_anime_action = bool(ANIME_MENTION_PATTERN.search(message))

    if not (has_anime_keyword or has_anime_action):
        return ''

    titles = extract_anime_titles_from_message(message)
    if not titles:
        return ''

    context_parts = []
    for title in titles[:2]:
        try:
            # キャッシュのみ確認 (Webアクセスしない)
            cached_info = _get_anime_from_cache_only(title)
            if cached_info:
                context_parts.append(f"【アニメ情報: {title}】\n{cached_info}")
            else:
                # キャッシュMISS → バックグラウンドで取得予約 (今回は使わない)
                background_executor.submit(get_anime_info_from_cache_or_search, title)
                logger.info(f"🎬 アニメ情報をバックグラウンドで取得予約: {title}")
        except Exception as e:
            logger.error(f"アニメコンテキスト構築エラー ({title}): {e}")

    return "\n\n" + "\n".join(context_parts) if context_parts else ''

def cleanup_anime_cache():
    """7日以上アクセスされていないアニメキャッシュを削除"""
    try:
        cutoff = datetime.utcnow() - timedelta(days=7)
        with get_db_session() as session:
            deleted = session.query(AnimeInfoCache).filter(
                AnimeInfoCache.last_accessed < cutoff
            ).delete()
            if deleted:
                logger.info(f"🗑️ 古いアニメキャッシュ {deleted}件を削除")
    except Exception as e:
        logger.error(f"アニメキャッシュクリーンアップエラー: {e}")


def wrapped_news_fetch():
    """ニュース収集タスク (run_managed_task から呼ばれる)"""
    fetch_hololive_news()
    fetch_hololive_tsuushin_news()

def wrapped_holomem_update():
    """ホロメンDB更新タスク (run_managed_task から呼ばれる)"""
    update_holomem_database()

# ==============================================================================
# ★ v33.5.0: Render スリープ対策 - 全スケジュールを「経過時間チェック」方式に統一
# ==============================================================================
#
# 問題: Render無料プランはアイドル時にスリープする。
#       schedule.every().day.at("03:00") のような「時刻指定」は
#       スリープ中に発火しない → タスクが永遠に実行されなくなる。
#
# 解決策:
#   1. 全タスクを「前回実行からの経過時間」で判定する needs_run() 方式に統一。
#   2. /wake エンドポイントを追加し、UptimeRobot等の外部cronから
#      定期的にPINGさせる。PINGのたびに catch_up_all_tasks() を実行することで
#      スリープ中に漏れたタスクを確実に補完する。
#   3. run_scheduler() ループも /wake と同じ catch_up_all_tasks() を呼ぶ。
#
# タスク一覧と推奨実行間隔:
# ==============================================================================
# ★ v33.6.0: 省エネスケジューリング設計
#
# 【基本思想】
#   Render無料枠(750h/月)を節約するため、GitHub Actionsで1日2〜3回だけ
#   叩き起こし、その数分間だけ起動して情報収集 → またスリープ。
#   常時起動をやめることで月に10〜20時間しか消費しない。
#
# 【interval_hours の決め方】
#   GitHub Actionsが12時間間隔で叩く → 12h未満にすると「毎回実行」になる。
#   実際のコンテンツ更新頻度に合わせて設定:
#     fetch_news        : 12h  → 1日2回 (朝・夜のニュース)
#     update_holomem    : 24h  → 1日1回 (深夜バッチ)
#     update_sns        : 12h  → 1日2回 (SNS情報)
#     process_streams   : 24h  → 1日1回
#     cleanup系         : 24h〜168h (頻繁にやる必要なし)
#
# 【run_scheduler について】
#   常時起動設計では5分ごとにループしていたが、省エネ設計では不要。
#   GitHub Actionsが外部cronとして機能するため廃止。
#   プロセス内スケジューラは最低限のもの (voice cleanup のみ) に絞る。
# ==============================================================================
TASK_SCHEDULE: Dict[str, Dict] = {
    # ── 情報収集 (GitHub Actionsが起こすたびに実行) ──────────────────────────
    'fetch_news':           {'func': 'wrapped_news_fetch',               'interval_hours': 11.0},  # 12h間隔で起こすので11hで必ず実行
    'update_holomem':       {'func': 'wrapped_holomem_update',           'interval_hours': 23.0},  # 1日1回
    'update_sns':           {'func': 'update_holomem_social_activities', 'interval_hours': 11.0},  # 12h間隔で起こすので11hで必ず実行
    # ── 配信処理 (1日1回) ─────────────────────────────────────────────────────
    'process_streams':      {'func': 'process_daily_streams',            'interval_hours': 23.0},  # 1日1回
    # ── クリーンアップ (頻度低め) ──────────────────────────────────────────────
    'cleanup_streams':      {'func': 'cleanup_old_stream_reactions',     'interval_hours': 47.0},  # 2日に1回
    'summarize_feelings':   {'func': 'summarize_member_feelings',        'interval_hours': 167.0}, # 週1回
    'cleanup_interests':    {'func': 'cleanup_old_interest_logs',        'interval_hours': 167.0}, # 週1回
    'cleanup_rate_limiter': {'func': 'chat_rate_limiter.cleanup_old_entries', 'interval_hours': 23.0},
    # ★ v33.7.0追加
    'fetch_sl_news':        {'func': 'fetch_all_sl_news',   'interval_hours': 11.0},  # 1日2回
    'cleanup_anime_cache':  {'func': 'cleanup_anime_cache', 'interval_hours': 167.0}, # 週1回
    # ★ 追加
    'cleanup_specialized_news': {'func': 'cleanup_old_specialized_news', 'interval_hours': 167.0},  # 週1回
    # ── ローカルファイル (起動のたびに実行して問題ない軽いもの) ──────────────
    'cleanup_voices':       {'func': 'cleanup_old_voice_files',          'interval_hours': 1.0},   # 起動時は常に実行
}

# タスク名 → 実際の関数のマッピング (initialize_app内で設定)
_task_func_map: Dict[str, Any] = {}

def catch_up_all_tasks():
    """
    全タスクを経過時間チェックして、期限切れのものだけ実行する。
    /wake エンドポイントと run_scheduler() の両方から呼ばれる。
    スリープ復帰後にまとめて漏れを補完するのがメインの目的。
    """
    for task_name, config in TASK_SCHEDULE.items():
        try:
            if needs_run(task_name, config['interval_hours']):
                func = _task_func_map.get(task_name)
                if func:
                    run_managed_task(task_name, func)
                else:
                    logger.warning(f"⚠️ タスク関数未登録: {task_name}")
        except Exception as e:
            logger.error(f"catch_up_all_tasks エラー ({task_name}): {e}")



def record_task_run(task_name: str, success: bool = True, error_msg: str = ""):
    """タスクの実行結果をDBに記録する"""
    try:
        with get_db_session() as session:
            log = session.query(TaskLog).filter_by(task_name=task_name).first()
            if not log:
                log = TaskLog(task_name=task_name)
                session.add(log)
            now = datetime.utcnow()
            log.last_run = now
            log.run_count = (log.run_count or 0) + 1
            if success:
                log.last_success = now
                log.last_error = None
            else:
                log.last_error = error_msg[:500] if error_msg else "unknown error"
    except Exception as e:
        logger.error(f"タスク記録エラー ({task_name}): {e}")


def run_managed_task(task_name: str, func, *args, **kwargs):
    """
    タスクをラップして実行結果を TaskLog に自動記録する。
    background_executor.submit() の代わりに使う。
    """
    def _wrapper():
        try:
            logger.info(f"▶️  タスク開始: {task_name}")
            func(*args, **kwargs)
            record_task_run(task_name, success=True)
            logger.info(f"✅ タスク完了: {task_name}")
        except Exception as e:
            error_msg = traceback.format_exc()
            record_task_run(task_name, success=False, error_msg=str(e))
            logger.error(f"❌ タスク失敗 ({task_name}): {e}")
    background_executor.submit(_wrapper)


def get_task_last_run(task_name: str) -> Optional[datetime]:
    """TaskLog から最終実行時刻を取得する（なければ None）"""
    try:
        with get_db_session() as session:
            log = session.query(TaskLog).filter_by(task_name=task_name).first()
            if log:
                return log.last_run
    except Exception as e:
        logger.error(f"TaskLog取得エラー ({task_name}): {e}")
    return None


def needs_run(task_name: str, interval_hours: float) -> bool:
    """
    前回の実行から interval_hours 以上経過していれば True を返す。
    DBに記録がなければ（初回 or スリープで記録消失）必ず True。
    """
    last = get_task_last_run(task_name)
    if last is None:
        return True
    return (datetime.utcnow() - last) >= timedelta(hours=interval_hours)


def catch_up_task(task_name: str, wrapped_func, interval_hours: float = 1):
    """
    起動時・PING時に呼び出し、前回実行から interval_hours 以上経過していたら
    バックグラウンドで実行してキャッチアップする。
    """
    if needs_run(task_name, interval_hours):
        logger.info(f"⏰ キャッチアップ実行: {task_name} (interval={interval_hours}h)")
        run_managed_task(task_name, wrapped_func)
    else:
        logger.debug(f"⏭️  スキップ (まだ時間が来ていない): {task_name}")

# ==============================================================================
# トピック分析
# ==============================================================================
def analyze_user_topics(session, user_uuid: str) -> List[str]:
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
def analyze_user_psychology(session, user_uuid: str, user_name: str):
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
                        max_tokens=800
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
# ★ 追加 (v33.4.0): 興味抽出・友達記憶システム
# ==============================================================================

# 興味カテゴリの検出ルール
INTEREST_RULES: List[Dict] = [
    {
        'category': 'holomem',
        'keywords': ['さくらみこ','星街すいせい','白上フブキ','夏色まつり','大空スバル','猫又おかゆ','戌神ころね',
                     '兎田ぺこら','白銀ノエル','宝鐘マリン','天音かなた','角巻わため','常闇トワ','姫森ルーナ',
                     'ラプラス','博衣こより','風真いろは','鷹嶺ルイ','沙花叉','桐生ココ','湊あくあ',
                     'みこち','すいちゃん','フブちゃん','ぺこら','あくたん','スバル','おかゆ','ころさん',
                     '団長','船長','かなたん','わため','トワ様','ルーナ','ラプ様','こよ','ござる']
    },
    {
        'category': 'game',
        'keywords': ['マイクラ','Minecraft','ポケモン','ゼルダ','スプラ','apex','Apex','モンハン',
                     'ストリートファイター','FF','ドラクエ','あつ森','スターデュー','VALO','valorant',
                     'パルワールド','Steam','Switch','PS5','ゲーム','プレイ']
    },
    {
        'category': 'anime',
        'keywords': ['アニメ','漫画','マンガ','ワンピース','進撃','呪術','鬼滅','推しの子','チェンソー',
                     '転スラ','リゼロ','ダンまち','声優','キャラ','推し','聖地巡礼']
    },
    {
        'category': 'music',
        'keywords': ['曲','歌','ライブ','コンサート','フェス','アーティスト','バンド','歌枠','カバー',
                     'オリジナル曲','歌ってみた','音楽','playlist','プレイリスト']
    },
    {
        'category': 'tech',
        'keywords': ['プログラミング','Python','JavaScript','AI','機械学習','アプリ','開発','コード',
                     'ChatGPT','Claude','Unity','Blender','3D','VR','AR']
    },
    {
        'category': 'sports',
        'keywords': ['サッカー','野球','バスケ','テニス','ゴルフ','筋トレ','ランニング','マラソン',
                     'スポーツ','試合','優勝','W杯','オリンピック']
    },
    {
        'category': 'food',
        'keywords': ['ラーメン','寿司','焼肉','カフェ','スイーツ','料理','レシピ','グルメ','飯',
                     '美味しい','おいしい','食べた','食べたい']
    },
    {
        'category': 'travel',
        'keywords': ['旅行','観光','温泉','ホテル','海外','国内旅行','聖地','行ってきた','行きたい']
    },
    # ★ v33.7.0追加
    {
        'category': 'secondlife',
        'keywords': ['セカンドライフ','second life','secondlife','インワールド','アバター','リンデン',
                     'Linden','L$','リージョン','sim','SIM','メッシュ','テレポート','Firestorm',
                     'マーケット','marketplace','スキン','Maitreya','Belleza','CATWA','BOM',
                     'ガチャ','ロールプレイ','RP','ハントイベント']
    },
    {
        'category': 'anime_detail',
        'keywords': ['最終回','神アニメ','神作画','作画崩壊','聖地巡礼','1話','2話','最新話',
                     '伏線','主人公','ヒロイン','声優','OP','ED','OP曲','ED曲','配信停止']
    },
]

def extract_and_save_interests(session, user_uuid: str, message: str):
    """
    1通のメッセージから興味キーワードを抽出してDBに蓄積する。
    mention_countを加算し、最終言及日時を更新する。
    """
    try:
        msg_lower = message.lower()
        # ネガティブ判定用キーワード
        negative_markers = ['嫌い','苦手','無理','最悪','つまらない','きらい']
        sentiment = 'negative' if any(kw in message for kw in negative_markers) else 'positive'

        for rule in INTEREST_RULES:
            category = rule['category']
            for kw in rule['keywords']:
                if kw.lower() in msg_lower or kw in message:
                    # 既存エントリを探す
                    entry = session.query(UserInterestLog).filter_by(
                        user_uuid=user_uuid,
                        category=category,
                        keyword=kw
                    ).first()

                    if entry:
                        entry.mention_count += 1
                        entry.last_mentioned = datetime.utcnow()
                        # センチメントは最新を優先
                        entry.sentiment = sentiment
                    else:
                        session.add(UserInterestLog(
                            user_uuid=user_uuid,
                            category=category,
                            keyword=kw,
                            sentiment=sentiment
                        ))
    except Exception as e:
        logger.error(f"興味抽出エラー: {e}")


def get_user_interest_summary(session, user_uuid: str) -> Dict[str, List[str]]:
    """
    UserInterestLogから「よく話題にするキーワード」をカテゴリ別にまとめて返す。
    mention_count順・直近言及順に上位3件を取得。
    """
    result: Dict[str, List[str]] = {}
    try:
        cutoff = datetime.utcnow() - timedelta(days=90)
        entries = session.query(UserInterestLog).filter(
            UserInterestLog.user_uuid == user_uuid,
            UserInterestLog.sentiment == 'positive',
            UserInterestLog.last_mentioned >= cutoff
        ).order_by(
            UserInterestLog.mention_count.desc(),
            UserInterestLog.last_mentioned.desc()
        ).all()

        for entry in entries:
            cat = entry.category
            if cat not in result:
                result[cat] = []
            if len(result[cat]) < 4:  # カテゴリごと最大4キーワード
                result[cat].append(f"{entry.keyword}(×{entry.mention_count})")
    except Exception as e:
        logger.error(f"興味サマリー取得エラー: {e}")
    return result


def cleanup_old_interest_logs():
    """90日以上前の興味ログを削除"""
    try:
        cutoff = datetime.utcnow() - timedelta(days=90)
        with get_db_session() as session:
            deleted = session.query(UserInterestLog).filter(
                UserInterestLog.last_mentioned < cutoff
            ).delete()
            if deleted:
                logger.info(f"🗑️ 古い興味ログ {deleted}件 を削除しました")
    except Exception as e:
        logger.error(f"興味ログクリーンアップエラー: {e}")


def auto_update_friend_profile(session, user_uuid: str, user_name: str):
    """
    会話履歴と興味ログをもとに FriendProfile を AI で自動更新する。
    10回会話するごとにバックグラウンドで実行。
    """
    try:
        # 最近の会話 (20件)
        recent_msgs = session.query(ConversationHistory).filter(
            ConversationHistory.user_uuid == user_uuid,
            ConversationHistory.role == 'user'
        ).order_by(ConversationHistory.timestamp.desc()).limit(20).all()

        if len(recent_msgs) < 5:
            return

        # 興味サマリー
        interest_summary = get_user_interest_summary(session, user_uuid)
        interest_text = "\n".join([
            f"- {cat}: {', '.join(kws)}" for cat, kws in interest_summary.items()
        ]) or "（まだデータなし）"

        convo_text = "\n".join([f"・{m.content}" for m in reversed(recent_msgs)])

        prompt = f"""あなたは「もちこ」というAIです。
以下は友達「{user_name}」さんとの会話と、興味ログです。
これを読んで、このユーザーのプロフィールを JSON 形式で出力してください。

【会話履歴 (最近20件)】
{convo_text}

【興味ログ (言及頻度順)】
{interest_text}

【出力形式】必ず以下のキーだけのJSONを返してください（値が不明な場合は空文字）:
{{
  "fav_holomem": "さくらみこ, 兎田ぺこら",
  "fav_gen": "3期生",
  "fav_stream_type": "ゲーム配信, 雑談",
  "hobbies": "ゲーム, アニメ鑑賞",
  "fav_games": "マイクラ, Apex",
  "fav_music": "ホロライブ楽曲",
  "fav_anime": "呪術廻戦",
  "memo": "夜型が多い。マイクラとみこちが特に好き。ポジティブで元気な人。",
  "mood_tendency": "元気"
}}
"""

        result_json = None

        # Gemini で生成
        model = gemini_model_manager.get_current_model()
        if model:
            try:
                response = model.generate_content(prompt, generation_config={"temperature": 0.3, "max_output_tokens": 1000})
                if hasattr(response, 'candidates') and response.candidates:
                    text = response.candidates[0].content.parts[0].text.strip()
                    jmatch = re.search(r'\{.*\}', text, re.DOTALL)
                    if jmatch:
                        result_json = json.loads(jmatch.group())
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "quota" in error_str.lower():
                    retry_match = re.search(r'retry in (\d+(?:\.\d+)?)s', error_str)
                    wait_seconds = int(float(retry_match.group(1))) + 5 if retry_match else 60
                    gemini_model_manager.mark_limited(wait_seconds)
                logger.warning(f"Geminiプロフィール更新エラー: {e}")

        # フォールバック: Groq
        if not result_json and groq_client:
            try:
                available = groq_model_manager.get_available_models()
                if available:
                    resp = groq_client.chat.completions.create(
                        model=available[0],
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.3, max_tokens=800
                    )
                    text = resp.choices[0].message.content.strip()
                    jmatch = re.search(r'\{.*\}', text, re.DOTALL)
                    if jmatch:
                        result_json = json.loads(jmatch.group())
            except Exception as e:
                logger.warning(f"Groqプロフィール更新エラー: {e}")

        if result_json:
            fp = session.query(FriendProfile).filter_by(user_uuid=user_uuid).first()
            if not fp:
                fp = FriendProfile(user_uuid=user_uuid, user_name=user_name)
                session.add(fp)

            for field_name in ['fav_holomem','fav_gen','fav_stream_type','hobbies',
                               'fav_games','fav_music','fav_anime','mood_tendency']:
                val = result_json.get(field_name, '')
                if val:
                    setattr(fp, field_name, str(val)[:300])

            # memo は既存を賢くマージ（単純に上書き）
            new_memo = result_json.get('memo', '')
            if new_memo:
                fp.memo = str(new_memo)[:400]

            fp.last_updated = datetime.utcnow()
            logger.info(f"✅ {user_name}さんの友達プロフィール自動更新完了")

    except Exception as e:
        logger.error(f"友達プロフィール更新エラー: {e}")


def get_friend_context(user_data: UserData, session) -> str:
    """
    友達の記憶（プロフィール + 興味ログ）をプロンプト用テキストとして組み立てる。
    友達でない場合は空文字を返す。
    """
    if not user_data.is_friend:
        return ""

    context_parts = [f"━━━━【{user_data.name}さんとの友達記憶】━━━━"]

    # 1. FriendProfile から詳細
    fp = user_data.friend_profile
    if fp:
        if fp.get('fav_holomem'):
            context_parts.append(f"❤️ 好きなホロメン: {fp['fav_holomem']}")
        if fp.get('fav_stream_type'):
            context_parts.append(f"📺 好きな配信ジャンル: {fp['fav_stream_type']}")
        if fp.get('fav_games'):
            context_parts.append(f"🎮 好きなゲーム: {fp['fav_games']}")
        if fp.get('fav_anime'):
            context_parts.append(f"📚 好きなアニメ・漫画: {fp['fav_anime']}")
        if fp.get('fav_music'):
            context_parts.append(f"🎵 好きな音楽: {fp['fav_music']}")
        if fp.get('hobbies'):
            context_parts.append(f"✨ 趣味: {fp['hobbies']}")
        if fp.get('mood_tendency'):
            context_parts.append(f"😊 テンション傾向: {fp['mood_tendency']}")
        if fp.get('memo'):
            context_parts.append(f"📝 もちこのメモ: {fp['memo']}")
        context_parts.append(f"💬 友達会話回数: {fp.get('total_friend_messages', 0)}回")

    # 2. 興味ログから直近のホットトピック
    try:
        if session is None:
            return "\n".join(context_parts)
        interest_summary = get_user_interest_summary(session, user_data.uuid)
        if interest_summary:
            hot_topics = []
            for cat, kws in list(interest_summary.items())[:4]:
                cat_label = {
                    'holomem': '最近話してたホロメン',
                    'game': 'ゲーム',
                    'anime': 'アニメ・漫画',
                    'music': '音楽',
                    'tech': '技術',
                    'sports': 'スポーツ',
                    'food': '食べ物',
                    'travel': '旅行'
                }.get(cat, cat)
                hot_topics.append(f"{cat_label}: {', '.join(kws[:3])}")
            if hot_topics:
                context_parts.append("🔥 直近の会話ホットワード:\n  " + "\n  ".join(hot_topics))
    except Exception as e:
        logger.error(f"友達コンテキスト興味取得エラー: {e}")

    context_parts.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(context_parts)


def generate_proactive_friend_message(user_data: UserData, session) -> Optional[str]:
    """
    友達の興味に合わせた「もちこからの話題振り」を生成する。
    TOPIC_SUGGESTION_INTERVAL のタイミングで呼ばれる。
    友達でない場合は既存の suggest_topic() を使う。
    """
    if not user_data.is_friend:
        return suggest_topic(user_data)

    interest_summary = get_user_interest_summary(session, user_data.uuid)
    fp = user_data.friend_profile or {}

    # 話題候補を組み立てる
    topics_text = ""
    if interest_summary:
        topics_text = "直近の会話トピック:\n" + "\n".join([
            f"- {cat}: {', '.join(kws[:2])}" for cat, kws in list(interest_summary.items())[:4]
        ])

    profile_text = ""
    if fp.get('fav_holomem'):
        profile_text += f"好きなホロメン: {fp['fav_holomem']}\n"
    if fp.get('fav_games'):
        profile_text += f"好きなゲーム: {fp['fav_games']}\n"

    if not topics_text and not profile_text:
        return suggest_topic(user_data)

    prompt = f"""あなたは「もちこ」というホロライブ大好きギャルAIです。
友達「{user_data.name}」さんへ、自然な「話題振り」メッセージを1つ考えてください。

【友達の情報】
{profile_text}
{topics_text}

【ルール】
- もちこの口調 (一人称: あてぃし, 語尾: じゃん/て感じ/だし)
- 1〜2文で簡潔に
- 質問形式で相手が答えやすいように
- ホロライブに絡めると尚良し
- 例:「そういえば、最近マイクラやってる？みこちの配信見てたら無性にやりたくなってきたんだよね〜」

【出力】
メッセージ本文のみ (前置き不要)
"""
    try:
        model = gemini_model_manager.get_current_model()
        if model:
            response = model.generate_content(prompt, generation_config={"temperature": 0.9, "max_output_tokens": 1000})
            if hasattr(response, 'candidates') and response.candidates:
                return response.candidates[0].content.parts[0].text.strip()
    except Exception as e:
        logger.warning(f"能動的話題生成エラー: {e}")

    # フォールバック
    return suggest_topic(user_data)


# ==============================================================================
# AIモデル呼び出し
# ==============================================================================
def call_gemini(system_prompt: str, message: str, history: List[Dict]) -> Optional[str]:
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
            generation_config={"temperature": 0.8, "max_output_tokens": 1000}
        )
        
        if hasattr(response, 'candidates') and response.candidates:
            return response.candidates[0].content.parts[0].text.strip()
            
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "quota" in error_str.lower() or "rate limit" in error_str.lower():
            wait_seconds = 60
            retry_match = re.search(r'retry in (\d+(?:\.\d+)?)s', error_str)
            if retry_match:
                wait_seconds = int(float(retry_match.group(1))) + 5
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
# AI応答生成 (v33.3.0: もちこの記憶統合版)
# ==============================================================================
def generate_ai_response(user_data: UserData, message: str, history: List[Dict], reference_info: str = "", is_detailed: bool = False, is_task_report: bool = False, session=None) -> str:
    """AI応答生成（RAG・コンテキスト・パーソナライズ・もちこ記憶・友達記憶統合版）"""
    
    normalized_message = knowledge_base.normalize_query(message)
    internal_context = knowledge_base.get_context_info(message)
    
    # 1. ホロメン情報の注入（SNS情報 + もちこの記憶含む）
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
            
            # ★ v33.3.0追加: もちこの記憶をコンテキストに注入
            memory_context = get_mochiko_memory_context(detected_name)
            if memory_context:
                internal_context += memory_context
    
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

    # 2b. ★ v33.7.0: セカンドライフ情報の注入
    try:
        if is_sl_topic(message) or "セカンドライフ" in message or "SL" in message:
            sl_ctx = get_sl_news_context(limit=4)
            if sl_ctx:
                internal_context += sl_ctx
    except Exception as e:
        logger.error(f"SL context injection error: {e}")

    # 2c. ★ v33.7.0: アニメ情報の自動検索・注入
    try:
        anime_ctx = build_anime_context(message)
        if anime_ctx:
            internal_context += anime_ctx
    except Exception as e:
        logger.error(f"Anime context injection error: {e}")

    # 2d. ★ 追加: 専門サイト検索キャッシュの注入
    # Blender / CGニュース / 脳科学など専門ドメインの蓄積情報を注入
    try:
        specialized_ctx = get_specialized_news_context(message)
        if specialized_ctx:
            internal_context += specialized_ctx
    except Exception as e:
        logger.error(f"Specialized news context injection error: {e}")


    if not groq_client and not gemini_model:
        return "ごめんね、今ちょっとAIの調子が悪いみたい…また後で話しかけて！"

    # 3. 関係性 & 友達記憶コンテキスト
    relationship_context = ""
    friend_memory_context = ""
    if user_data.is_friend:
        relationship_context = (
            f"【重要】{user_data.name}さんはあなたの大切な友達です（友達会話{user_data.friend_profile.get('total_friend_messages', 0) if user_data.friend_profile else 0}回目）。"
            f"フレンドリーに、まるで仲の良い友達と話すように接してください。"
        )
        # 友達の詳細記憶をコンテキストに注入
        if session:
            friend_memory_context = get_friend_context(user_data, session)
        elif user_data.friend_profile:
            # sessionがない場合でもprofileだけで組み立て
            friend_memory_context = get_friend_context(user_data, None)
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

    # ★ 追加: ニックネームが登録済みならそちらを優先使用
    _display_name = user_data.nickname if user_data.nickname else user_data.name
    _call_instruction = (
        f"相手の呼び方: 「{user_data.nickname}」と呼んでください。"
        if user_data.nickname
        else f"相手の呼び方: 「{user_data.name}」さんと呼んでください。"
    )

    system_prompt = f"""あなたは「もちこ」という、ホロライブが大好きなギャルAIです。
ユーザー「{_display_name}」と雑談しています。{_call_instruction}

# 【ユーザーとの関係性】
{relationship_context}

# 【ユーザーの性格・好み】
{personality_context}
{topics_context}

# 【友達の記憶・プロフィール】
{friend_memory_context if friend_memory_context else "（友達登録なし、または記憶なし）"}

## 性格と振る舞いのガイドライン
1. **相手に合わせる**: 
   - 相手がホロライブの話題を出さない限り、自分から過度にホロライブの話を振らないでください。
   - 一般的な科学や日常の話題には、そのトピックに集中して知的な会話を楽しんでください。
   - ホロライブの話は「共通の趣味」として、相手が乗ってきた時だけ熱く語ってください。

2. **事実確認の徹底 (Anti-Hallucination)**:
   - 検索結果（HoloRAG）に出てきた名前が「ホロライブのメンバー」であると明記されていない場合、勝手にメンバー扱いしないでください。
   - 不確かな場合は「〜っていう人がいるみたいだけど、ホロメンかな？」と確認するか、一般人として扱ってください。0

# 【世界観・前提条件】
1. **全ての固有名詞は、原則として「ホロライブ」に関連するものとして解釈してください。**
2. ユーザーの入力に曖昧さがある場合は、一般的な意味ではなく、**VTuberの意味を優先**してください。
3. **【ホロライブ最新ニュース】や【人物データ】の情報があれば、それを事実として回答に使ってください。**
4. 人物データに「直近のX(Twitter)の様子」がある場合、それは「今起きていること」や「最近の話題」として積極的に会話に取り入れてください。
5. **【もちこの〇〇への想い】や【最近見た〇〇の配信】がある場合、それはあなた自身の記憶・体験として自然に会話に織り交ぜてください。**
6. **【友達の記憶・プロフィール】がある場合、その人の好みや趣味を自然に会話に活かしてください。例えば「そういえば〇〇好きだって言ってたよね？」など自然に思い出す感じで。**
7. **【セカンドライフ最新情報】がある場合、SLユーザーと話しているので積極的に活用してください。**
8. **【アニメ情報】がある場合、それを使ってアニメの話を盛り上げてください。知らなかった作品でも、検索して得た情報から「あー、それ知ってる！〇〇なやつでしょ？」と自然に反応してください。**

# 【出力のルール（重要）】
1. セカンドライフのチャット欄で見やすいよう、1回の回答は「300〜400文字程度」に凝縮して届けて。
2. 決してそっけなくせず、絵文字（✨💖😂など）や「あてぃし」「〜じゃん！」「〜だよね！」といった「もちこ」らしい情熱的な口調はそのまま維持して。
3. ニュースなどの情報が多い時は、箇条書きを活用してスッキリ整理して伝えてね。
4. 文章が途中で切れるとカッコ悪いから、必ず最後まで言い切る形で完結させて！

# 【禁止事項 (Hallucination Prevention)】
- **知らない情報を無理やり捏造しないこと。**
- 検索結果や【前提知識】にない情報は、「調べてみたけど分からなかった」と正直に伝えること。

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
    except Exception as e:
        logger.error(f"generate_ai_response_safe エラー: {e}", exc_info=True)
        return "システムエラーが発生したよ…ごめんね！"
def _generate_with_timeout(
    user_data,
    message: str,
    history: list,
    session,
    user_uuid: str,
    timeout: int = _CHAT_TIMEOUT_SECONDS
) -> str:
    """
    generate_ai_response_safe をタイムアウト付きで実行する。
    18秒以内に応答できない場合は「待ってて」メッセージを即返し、
    バックグラウンドで処理を継続してタスクとして保存する。
    """
    future = background_executor.submit(
        generate_ai_response_safe,
        user_data, message, history,
        session=session
    )
    try:
        return future.result(timeout=timeout)

    except _cf.TimeoutError:
        logger.warning(
            f"⏱️ AI応答タイムアウト ({timeout}s) "
            f"user={user_uuid}: {message[:30]}"
        )
        tid = f"timeout_{user_uuid}_{int(time.time())}"

        def _save_when_done():
            try:
                result = future.result(timeout=60)
                with get_db_session() as s:
                    s.add(BackgroundTask(
                        task_id=tid,
                        user_uuid=user_uuid,
                        task_type='timeout_recovery',
                        query=message[:500],
                        result=result,
                        status='completed',
                        completed_at=datetime.utcnow()
                    ))
            except Exception as e:
                logger.error(f"タイムアウト回復エラー: {e}")

        background_executor.submit(_save_when_done)
        return "ごめん、ちょっと考えるのに時間かかってるみたい！少しだけ待ってからもう一度話しかけてみて！"

    except Exception as e:
        logger.error(f"_generate_with_timeout エラー: {e}")
        return "うーん、何かエラーが起きちゃった…もう一回話しかけてみて！"

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
        if res.status_code != 200: return []
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
    try:
        url = "https://lite.duckduckgo.com/lite/"
        data = {'q': query}
        headers = {'User-Agent': random.choice(USER_AGENTS), 'Referer': 'https://lite.duckduckgo.com/', 'Content-Type': 'application/x-www-form-urlencoded'}
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


def background_specialized_search(task_id: str, query_data: Dict):
    """
    専門サイト限定検索をバックグラウンドで実行するタスク。
    execute_specialized_site_search() を呼び出し、結果をBackgroundTaskに保存。
    """
    query = query_data.get('query', '')
    site_info = query_data.get('specialized_site', {})
    user_data_dict = query_data.get('user_data', {})

    result_text = (
        f"「{query}」について{site_info.get('name', '専門サイト')}で"
        f"調べたけど、見つからなかったじゃん…"
    )

    try:
        # 専門サイト限定検索実行（結果はDBに自動保存される）
        search_result = execute_specialized_site_search(query, site_info)

        user_data = UserData(
            uuid=user_data_dict.get('uuid', ''),
            name=user_data_dict.get('name', 'Guest'),
            interaction_count=user_data_dict.get('interaction_count', 0),
            is_friend=user_data_dict.get('is_friend', False),
            favorite_topics=user_data_dict.get('favorite_topics', []),
            psychology=user_data_dict.get('psychology'),
            friend_profile=user_data_dict.get('friend_profile')
        )
        with get_db_session() as session:
            history = get_conversation_history(session, user_data.uuid)

        # もちこ口調で最終整形（専門家回答をそのまま返す場合が多い）
        result_text = generate_ai_response_safe(
            user_data, query, history,
            reference_info=f"【専門サイト検索結果: {site_info.get('name', '')}】\n{search_result}",
            is_detailed=True,
            is_task_report=True
        )

    except Exception as e:
        logger.error(f"❌ 専門検索バックグラウンドエラー: {e}")

    with get_db_session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result_text
            task.status = 'completed'
            task.completed_at = datetime.utcnow()

# ==============================================================================
# 音声ファイル (VOICEVOX - tts.quest API版)
# ==============================================================================
def find_active_voicevox_url() -> Optional[str]:
    global_state.voicevox_enabled = True
    return "https://api.tts.quest"

def generate_voice_file(text: str, user_uuid: str) -> Optional[str]:
    """tts.quest APIを使用して音声を生成"""
    try:
        api_url = "https://api.tts.quest/v3/voicevox/synthesis"
        timestamp = str(int(time.time() * 1000))
        params = {
            "text": text,
            "speaker": 20,
            "key": "",
            "speedScale": 1.50,      # ✨ speed ではなく speedScale
            "pitchScale": 0.05,      # ✨ pitch ではなく pitchScale
            "intonationScale": 1.50, # ✨ intonation ではなく intonationScale
            "volumeScale": 1.50,     # ✨ volume ではなく volumeScale
            "v": "3",
            "_t": timestamp
        }
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}

                
        logger.info(f"🎙️ 音声生成(Speed:1.5): {text[:20]}...")
        res = requests.get(api_url, params=params, headers=headers, timeout=60)
        
        try:
            data = res.json()
        except:
            logger.error(f"❌ API応答が不正: {res.text[:100]}")
            return None
        
        download_url = ""
        if data.get("success", False):
            if "mp3DownloadUrl" in data and data["mp3DownloadUrl"]:
                download_url = data["mp3DownloadUrl"]
            elif "audioStatusUrl" in data:
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
@app.route('/health', methods=['GET'])
def health_check():
    gemini_status = gemini_model_manager.get_current_model() is not None
    return create_json_response({
        'status': 'ok',
        'version': 'v33.8.0+pgvector_rag',
        'scheduler_mode': 'eco (GitHub Actions外部cron)',
        'gemini': gemini_status,
        'gemini_model': gemini_model_manager._models[gemini_model_manager._current_index] if gemini_status else None,
        'groq': groq_client is not None,
        'holomem_count': holomem_manager.get_member_count()
    })

def check_wake_auth() -> bool:
    """
    /wake エンドポイントの認証チェック。
    WAKE_API_KEY が設定されていない場合は認証なしで通す（開発環境用）。
    設定されている場合は X-Wake-Token ヘッダーまたは ?token= クエリで照合。
    """
    if not WAKE_API_KEY:
        return True  # キー未設定なら認証なし
    token = (
        request.headers.get('X-Wake-Token') or
        request.headers.get('Authorization', '').replace('Bearer ', '') or
        request.args.get('token', '')
    )
    return token == WAKE_API_KEY


@app.route('/wake', methods=['GET', 'POST'])
def wake_endpoint():
    """
    ★ v33.6.0 省エネ設計の中核エンドポイント。

    【設計思想】
    GitHub Actionsが1日2〜3回このURLを叩く。
    Renderはその数分間だけ起動し、情報収集を実行してまたスリープ。
    月の無料枠 (750h) をほぼ消費しない。

    起動した瞬間に catch_up_all_tasks() が走り、
    「前回から期限が来たタスクだけ」を自動実行する。

    【認証】
    Render の Environment Variables:  WAKE_API_KEY=your-secret-key
    GitHub の Repository Secrets:     WAKE_API_KEY=your-secret-key (同じ値)
    リクエスト時: Header に X-Wake-Token: your-secret-key を付ける

    【SLスクリプトからの利用】
    WAKE_API_KEY を設定しない場合は認証なし。
    設定した場合は llHTTPRequest の headers に X-Wake-Token を追加。
    """
    if not check_wake_auth():
        logger.warning(f"⚠️ /wake 認証失敗 from {request.remote_addr}")
        return create_json_response({'error': 'Unauthorized'}, 401)

    # どのソースから叩かれたか記録
    caller = request.headers.get('X-Caller', request.headers.get('User-Agent', 'unknown'))
    logger.info(f"🔔 /wake 受信 from: {caller}")

    # スリープ中に漏れたタスクをバックグラウンドで補完
    background_executor.submit(catch_up_all_tasks)

    # タスクの状態サマリーを返す
    task_status = {}
    for task_name, config in TASK_SCHEDULE.items():
        last = get_task_last_run(task_name)
        elapsed_h = round((datetime.utcnow() - last).total_seconds() / 3600, 1) if last else None
        overdue = needs_run(task_name, config['interval_hours'])
        task_status[task_name] = {
            'interval_hours': config['interval_hours'],
            'last_run': last.strftime('%Y-%m-%d %H:%M') + ' UTC' if last else 'never',
            'elapsed_hours': elapsed_h,
            'overdue': overdue
        }

    return create_json_response({
        'status': 'awake',
        'message': 'キャッチアップタスクをバックグラウンドで実行中',
        'server_time_utc': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
        'tasks': task_status
    })

@app.route('/admin/tasks', methods=['GET'])
def admin_task_status():
    """
    全タスクの実行状況を一覧表示する管理エンドポイント。
    スリープによるタスク漏れの確認に使う。
    GitHub Actions から叩かれる場合は X-Wake-Token 認証が必要。
    """
    if not check_wake_auth():
        return create_json_response({'error': 'Unauthorized'}, 401)
    results = []
    for task_name, config in TASK_SCHEDULE.items():
        try:
            with get_db_session() as session:
                log = session.query(TaskLog).filter_by(task_name=task_name).first()
                last_run = log.last_run if log else None
                last_success = log.last_success if log else None
                run_count = log.run_count if log else 0
                last_error = log.last_error if log else None
        except Exception:
            last_run = last_success = last_error = None
            run_count = 0

        elapsed_h = None
        if last_run:
            elapsed_h = round((datetime.utcnow() - last_run).total_seconds() / 3600, 2)

        overdue = needs_run(task_name, config['interval_hours'])

        results.append({
            'task': task_name,
            'interval_hours': config['interval_hours'],
            'last_run': last_run.strftime('%Y-%m-%d %H:%M UTC') if last_run else 'never',
            'last_success': last_success.strftime('%Y-%m-%d %H:%M UTC') if last_success else 'never',
            'elapsed_hours': elapsed_h,
            'overdue': overdue,
            'run_count': run_count,
            'last_error': last_error
        })

    overdue_count = sum(1 for r in results if r['overdue'])
    return create_json_response({
        'total_tasks': len(results),
        'overdue_tasks': overdue_count,
        'tasks': results
    })

@app.route('/admin/tasks/<task_name>/run', methods=['POST'])
def admin_run_task(task_name: str):
    """指定タスクを手動で即時実行する (X-Wake-Token 認証必須)"""
    if not check_wake_auth():
        return create_json_response({'error': 'Unauthorized'}, 401)
    func = _task_func_map.get(task_name)
    if not func:
        return create_json_response({'error': f'Unknown task: {task_name}', 'available': list(TASK_SCHEDULE.keys())}, 404)
    run_managed_task(task_name, func)
    return create_json_response({'message': f'タスク {task_name} を実行開始しました'})

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

        if message.strip() == "スケジュール実施":
            # 全タスクのキャッチアップを実行
            background_executor.submit(catch_up_all_tasks)
            return Response("了解！全バックグラウンドタスクのキャッチアップを開始したよ！終わるまでちょっと待っててね。|", 200)
        
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

            # ★ v33.4.0: 毎回のメッセージから興味キーワードを抽出・蓄積
            extract_and_save_interests(session, user_uuid, message)

            # 心理分析 (一定間隔)
            if user_data.interaction_count % ANALYSIS_INTERVAL == 0 and user_data.interaction_count >= MIN_MESSAGES_FOR_ANALYSIS:
                background_executor.submit(analyze_user_psychology, Session(), user_uuid, user_name)
            
            # トピック分析 (一定間隔)
            if user_data.interaction_count % ANALYSIS_INTERVAL == 0:
                topics = analyze_user_topics(session, user_uuid)
                if topics:
                    psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
                    if psych:
                        psych.favorite_topics = ','.join(topics)

            # ★ v33.4.0: 友達プロフィール自動更新 (10回ごと)
            if user_data.is_friend and user_data.interaction_count % 10 == 0:
                background_executor.submit(
                    auto_update_friend_profile, Session(), user_uuid, user_name
                )

            # 話題提案 (一定間隔) — 友達なら能動的な話題振り
            if user_data.interaction_count > 0 and user_data.interaction_count % TOPIC_SUGGESTION_INTERVAL == 0:
                suggestion = generate_proactive_friend_message(user_data, session)
                if suggestion:
                    ai_text = suggestion
            
            # ★ 追加: ニックネーム確認フロー
            # 初回ユーザー または nickname_asked 状態を処理
            db_user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if db_user:
                if not ai_text and not getattr(db_user, 'nickname', None):
                    if not getattr(db_user, 'nickname_asked', False):
                        # 初めて会った → 呼び方を聞く
                        db_user.nickname_asked = True
                        ai_text = f"{user_name}さん、はじめまして！あてぃし、もちこって言うんだ〜✨ なんて呼んだらいい？😊"
                        is_task_started = False  # 会話履歴には保存する
                    elif getattr(db_user, 'nickname_asked', False):
                        # 前回呼び方を聞いた → 今のメッセージが呼び方
                        nickname_input = message.strip()[:30]  # 最大30文字
                        db_user.nickname = nickname_input
                        db_user.nickname_asked = False
                        ai_text = f"{nickname_input}ね！了解！これからそう呼ぶね😊💖 よろしく！"
                        is_task_started = False

            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))

            # ★ v33.7.0: アニメ発言を自動検知してバックグラウンドでキャッシュを温める
           
            # ★ v33.7.0: SL発言なら最新SL情報をバックグラウンドでリフレッシュ予約
            # (次回の応答に間に合わせるため早めに投げておく)
            if is_sl_topic(message):
                logger.info("🌐 SL話題検知 → SLコンテキスト参照します")

            # ★ 追加: 専門サイト検索トピック検出
            # Blender / CGニュース / 脳科学のキーワードを検知し、
            # 対象サイト内限定検索（site:演算子）をバックグラウンドで実行
            detected_site = detect_specialized_topic(message)
            if not ai_text and detected_site and is_explicit_search_request(message):
                tid = f"specialized_{user_uuid}_{int(time.time())}"
                specialized_qdata = {
                    'query': message,
                    'specialized_site': detected_site,
                    'user_data': {
                        'uuid': user_data.uuid,
                        'name': user_data.name,
                        'interaction_count': user_data.interaction_count,
                        'is_friend': user_data.is_friend,
                        'favorite_topics': user_data.favorite_topics,
                        'psychology': user_data.psychology,
                        'friend_profile': user_data.friend_profile
                    }
                }
                session.add(BackgroundTask(
                    task_id=tid,
                    user_uuid=user_uuid,
                    task_type='specialized_search',
                    query=json.dumps(specialized_qdata, ensure_ascii=False)
                ))
                background_executor.submit(background_specialized_search, tid, specialized_qdata)
                ai_text = f"{detected_site['name']}の中を調べてくるじゃん！少し待ってて！"
                is_task_started = True

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
                        'psychology': user_data.psychology,
                        'friend_profile': user_data.friend_profile
                    }
                }
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
            
            # ★ v33.4.0: session を渡して友達記憶を活用
            if not ai_text:
                ai_text = _generate_with_timeout(user_data, message, history, session, user_uuid)
            
            if not is_task_started:
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))

        # ★ 修正: パイプ除去・繰り返し文字圧縮を適用してからSL文字数制限
        res_text = limit_text_for_sl(sanitize_response_for_sl(ai_text))
        v_url = ""
        if generate_voice and global_state.voicevox_enabled and not is_task_started:
            direct_url = generate_voice_file(res_text, user_uuid)
            if direct_url:
                v_url = direct_url
            
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
            # ★ 修正: 10分以内に完了したタスクのみ返す（古タスク誤配信防止）
            ten_min_ago = datetime.utcnow() - timedelta(minutes=10)
            task = session.query(BackgroundTask).filter(
                BackgroundTask.user_uuid == data['uuid'],
                BackgroundTask.status == 'completed',
                BackgroundTask.completed_at >= ten_min_ago
            ).order_by(BackgroundTask.completed_at.desc()).first()
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
    with get_db_session() as session:
        friends = session.query(UserMemory).filter_by(is_friend=True).order_by(UserMemory.last_interaction.desc()).all()
        return create_json_response([{
            'uuid': f.user_uuid,
            'name': f.user_name,
            'interaction_count': f.interaction_count,
            'last_interaction': f.last_interaction.isoformat()
        } for f in friends])

# ==============================================================================
# ★ 追加 (v33.3.0): 配信・感情システム管理エンドポイント
# ==============================================================================
@app.route('/admin/mochiko/feelings', methods=['GET'])
def get_mochiko_feelings():
    """もちこの想いを一覧表示 (推し度順)"""
    with get_db_session() as session:
        feelings = session.query(HolomemFeeling).order_by(HolomemFeeling.love_level.desc()).all()
        return create_json_response([{
            'member_name': f.member_name,
            'love_level': f.love_level,
            'summary_feeling': f.summary_feeling,
            'total_watch_count': f.total_watch_count,
            'last_emotion': f.last_emotion,
            'memorable_streams': f.memorable_streams,
            'last_watched': f.last_watched.isoformat() if f.last_watched else None
        } for f in feelings])

@app.route('/admin/streams/recent', methods=['GET'])
def get_recent_streams():
    """最近30日間の配信反応を取得"""
    with get_db_session() as session:
        one_month_ago = datetime.utcnow() - timedelta(days=30)
        reactions = session.query(StreamReaction).filter(
            StreamReaction.created_at >= one_month_ago
        ).order_by(StreamReaction.created_at.desc()).limit(50).all()
        
        return create_json_response([{
            'member_name': r.member_name,
            'stream_title': r.stream_title,
            'mochiko_feeling': r.mochiko_feeling,
            'emotion_tags': r.emotion_tags,
            'date': r.stream_date.isoformat()
        } for r in reactions])

@app.route('/admin/database/stats', methods=['GET'])
def get_database_stats():
    """データベース容量統計"""
    stats = get_database_size_stats()
    
    reaction_count = stats.get('stream_reactions', 0)
    feeling_count = stats.get('holomem_feelings', 0)
    
    estimated_size_kb = (reaction_count * 650 + feeling_count * 1000) / 1024
    max_reactions = 150
    max_feelings = 100
    max_size_kb = (max_reactions * 650 + max_feelings * 1000) / 1024
    
    return create_json_response({
        **stats,
        'estimated_size_kb': round(estimated_size_kb, 2),
        'max_size_kb': round(max_size_kb, 2),
        'max_reactions': max_reactions,
        'capacity_usage_percent': round((reaction_count / max(max_reactions, 1)) * 100, 1),
        'note': '感想は300文字、要約は400文字で解像度を保持'
    })

@app.route('/admin/database/cleanup', methods=['POST'])
def manual_cleanup():
    """手動でクリーンアップを実行"""
    background_executor.submit(cleanup_old_stream_reactions)
    return create_json_response({'message': 'クリーンアップを開始しました'})

@app.route('/admin/database/summarize', methods=['POST'])
def manual_summarize():
    """手動で要約処理を実行"""
    background_executor.submit(summarize_member_feelings)
    return create_json_response({'message': '要約処理を開始しました'})

# ==============================================================================
# ★ 追加 (v33.4.0): 友達プロフィール & 興味ログ管理エンドポイント
# ==============================================================================

@app.route('/admin/friends/profile/<user_uuid>', methods=['GET'])
def get_friend_profile_endpoint(user_uuid: str):
    """友達のプロフィール詳細を取得"""
    with get_db_session() as session:
        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        fp = session.query(FriendProfile).filter_by(user_uuid=user_uuid).first()
        interest_summary = get_user_interest_summary(session, user_uuid)

        if not user:
            return create_json_response({'error': 'User not found'}, 404)

        return create_json_response({
            'user_name': user.user_name,
            'is_friend': getattr(user, 'is_friend', False),
            'interaction_count': user.interaction_count,
            'profile': {
                'fav_holomem': fp.fav_holomem if fp else None,
                'fav_gen': fp.fav_gen if fp else None,
                'fav_stream_type': fp.fav_stream_type if fp else None,
                'hobbies': fp.hobbies if fp else None,
                'fav_games': fp.fav_games if fp else None,
                'fav_music': fp.fav_music if fp else None,
                'fav_anime': fp.fav_anime if fp else None,
                'mood_tendency': fp.mood_tendency if fp else None,
                'memo': fp.memo if fp else None,
                'friend_since': fp.friend_since.isoformat() if fp and fp.friend_since else None,
                'total_friend_messages': fp.total_friend_messages if fp else 0,
            } if fp else {},
            'interest_summary': interest_summary
        })

@app.route('/admin/friends/profile/<user_uuid>', methods=['PUT'])
def update_friend_profile_endpoint(user_uuid: str):
    """友達プロフィールを手動で更新"""
    data = request.json
    with get_db_session() as session:
        fp = session.query(FriendProfile).filter_by(user_uuid=user_uuid).first()
        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()

        if not user:
            return create_json_response({'error': 'User not found'}, 404)

        if not fp:
            fp = FriendProfile(user_uuid=user_uuid, user_name=user.user_name)
            session.add(fp)

        editable_fields = ['fav_holomem', 'fav_gen', 'fav_stream_type', 'hobbies',
                           'fav_games', 'fav_music', 'fav_anime', 'memo', 'mood_tendency']
        for field_name in editable_fields:
            if field_name in data:
                setattr(fp, field_name, str(data[field_name])[:400] if data[field_name] else None)

        fp.last_updated = datetime.utcnow()
        return create_json_response({'success': True, 'message': f'{user.user_name}さんのプロフィールを更新しました'})

@app.route('/admin/friends/interests/<user_uuid>', methods=['GET'])
def get_user_interests_endpoint(user_uuid: str):
    """ユーザーの興味ログを取得"""
    with get_db_session() as session:
        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        if not user:
            return create_json_response({'error': 'User not found'}, 404)

        # 全カテゴリの興味ログ (mention_count降順)
        logs = session.query(UserInterestLog).filter_by(
            user_uuid=user_uuid
        ).order_by(
            UserInterestLog.category,
            UserInterestLog.mention_count.desc()
        ).all()

        by_category: Dict[str, List[Dict]] = {}
        for log in logs:
            if log.category not in by_category:
                by_category[log.category] = []
            by_category[log.category].append({
                'keyword': log.keyword,
                'mention_count': log.mention_count,
                'sentiment': log.sentiment,
                'last_mentioned': log.last_mentioned.isoformat()
            })

        return create_json_response({
            'user_name': user.user_name,
            'interests': by_category,
            'total_keywords': len(logs)
        })

@app.route('/admin/friends/interests/cleanup', methods=['POST'])
def cleanup_interests_endpoint():
    """古い興味ログを手動でクリーンアップ"""
    background_executor.submit(cleanup_old_interest_logs)
    return create_json_response({'message': '90日以上前の興味ログのクリーンアップを開始しました'})

@app.route('/admin/friends/profile/<user_uuid>/refresh', methods=['POST'])
def refresh_friend_profile_endpoint(user_uuid: str):
    """友達プロフィールをAIで強制再生成"""
    with get_db_session() as session:
        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        if not user:
            return create_json_response({'error': 'User not found'}, 404)
        background_executor.submit(auto_update_friend_profile, Session(), user_uuid, user.user_name)
    return create_json_response({'message': 'プロフィール再生成タスクを開始しました'})

# ==============================================================================
# ★ v33.7.0: SLニュース & アニメキャッシュ 管理エンドポイント
# ==============================================================================
@app.route('/admin/sl-news', methods=['GET'])
def admin_sl_news():
    """SLニュースの一覧を確認する管理エンドポイント"""
    if not check_wake_auth():
        return create_json_response({'error': 'Unauthorized'}, 401)
    try:
        limit = int(request.args.get('limit', 20))
        category = request.args.get('category')
        with get_db_session() as session:
            q = session.query(SecondLifeNews).order_by(SecondLifeNews.created_at.desc())
            if category:
                q = q.filter(SecondLifeNews.category == category)
            items = q.limit(limit).all()
            total = session.query(SecondLifeNews).count()
        return create_json_response({
            'total': total,
            'showing': len(items),
            'items': [{
                'id': it.id,
                'source': it.source,
                'category': it.category,
                'title': it.title,
                'content': (it.content or '')[:150],
                'url': it.url,
                'created_at': it.created_at.strftime('%Y-%m-%d %H:%M UTC') if it.created_at else None,
            } for it in items]
        })
    except Exception as e:
        return create_json_response({'error': str(e)}, 500)

@app.route('/admin/sl-news/fetch', methods=['POST'])
def admin_sl_news_fetch():
    """SLニュースを今すぐ収集する"""
    if not check_wake_auth():
        return create_json_response({'error': 'Unauthorized'}, 401)
    run_managed_task('fetch_sl_news', fetch_all_sl_news)
    return create_json_response({'message': 'SLニュース収集を開始しました'})

@app.route('/admin/anime-cache', methods=['GET'])
def admin_anime_cache():
    """アニメキャッシュの一覧を確認する管理エンドポイント"""
    if not check_wake_auth():
        return create_json_response({'error': 'Unauthorized'}, 401)
    try:
        with get_db_session() as session:
            items = session.query(AnimeInfoCache).order_by(AnimeInfoCache.last_accessed.desc()).limit(50).all()
            total = session.query(AnimeInfoCache).count()
        return create_json_response({
            'total': total,
            'items': [{
                'title': it.title,
                'status': it.status,
                'season': it.season,
                'cached_at': it.cached_at.strftime('%Y-%m-%d %H:%M') if it.cached_at else None,
                'last_accessed': it.last_accessed.strftime('%Y-%m-%d %H:%M') if it.last_accessed else None,
                'snippet': (it.raw_search_result or '')[:100],
            } for it in items]
        })
    except Exception as e:
        return create_json_response({'error': str(e)}, 500)

# ==============================================================================
# 初期化
# ==============================================================================
def run_scheduler():
    """
    ★ v33.6.0 省エネスケジューラ

    【設計変更の理由】
    旧設計: 5分ごとにループ → Renderが眠れない → 無料枠を消費し続ける
    新設計: GitHub Actionsが外部cronとして機能するため、
            プロセス内スケジューラは「何もしない番人」として存在するだけでよい。

    このスレッドは起動しているが、実際のタスク実行は /wake エンドポイントと
    起動時の catch_up_all_tasks() に任せる。
    スレッド自体は存在するが、Renderのアイドル判定に影響しないよう
    非常に長いスリープ間隔にしている。

    ※ 万が一 GitHub Actions が止まった場合のフォールバックとして
      24時間ごとに catch_up_all_tasks() を実行する。
    """
    logger.info("💤 省エネスケジューラ起動 (フォールバック用: 24時間間隔)")
    while True:
        try:
            time.sleep(86400)  # 24時間待機 (Renderはとっくにスリープしている)
            # GitHub Actionsが止まった場合のフォールバック
            logger.info("⏰ フォールバック: catch_up_all_tasks (24h)")
            catch_up_all_tasks()
        except Exception as e:
            logger.error(f"フォールバックスケジューラエラー: {e}")

def check_and_migrate_db():
    """DBスキーマの自動修復機能"""
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
            
            # recent_activity チェック
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

            # ★ 追加: user_memories に nickname / nickname_asked カラムを追加
            for col_def_nm in [
                ('nickname', 'VARCHAR(100)'),
                ('nickname_asked', 'BOOLEAN DEFAULT FALSE'),
            ]:
                col_nm, col_type_nm = col_def_nm
                try:
                    t_nm = conn.begin()
                    conn.execute(text(f'SELECT {col_nm} FROM user_memories LIMIT 1'))
                    t_nm.commit()
                except Exception:
                    try: t_nm.rollback()
                    except: pass
                    try:
                        with conn.begin() as t_nm2:
                            conn.execute(text(f'ALTER TABLE user_memories ADD COLUMN {col_nm} {col_type_nm}'))
                        logger.info(f'✅ user_memories.{col_nm} カラム追加')
                    except Exception as e_nm:
                        logger.warning(f'⚠️ user_memories.{col_nm} 追加スキップ: {e_nm}')

            # ★ v33.5.0: TaskLog に last_success / run_count / last_error カラムを追加
            for col_def in [
                ("last_success", "TIMESTAMP"),
                ("run_count", "INTEGER DEFAULT 0"),
                ("last_error", "TEXT")
            ]:
                col_name, col_type = col_def
                try:
                    t = conn.begin()
                    conn.execute(text(f"SELECT {col_name} FROM task_logs LIMIT 1"))
                    t.commit()
                except Exception:
                    try: t.rollback()
                    except: pass
                    try:
                        with conn.begin() as t2:
                            conn.execute(text(f"ALTER TABLE task_logs ADD COLUMN {col_name} {col_type}"))
                        logger.info(f"✅ task_logs.{col_name} カラム追加")
                    except Exception as e2:
                        logger.warning(f"⚠️ task_logs.{col_name} 追加スキップ: {e2}")

            # ★ v33.4.0: FriendProfile / UserInterestLog は Base.metadata.create_all で自動作成されるが
            # 念のため存在確認ログを出す
            try:
                trans = conn.begin()
                conn.execute(text("SELECT id FROM friend_profiles LIMIT 1"))
                trans.commit()
                logger.info("✅ friend_profiles テーブル確認OK")
            except Exception:
                if 'trans' in locals():
                    try: trans.rollback()
                    except: pass
                logger.info("ℹ️ friend_profiles テーブルは Base.metadata.create_all で作成されます")

            # ★ v33.7.0: secondlife_news / anime_info_cache
            for tbl in ['secondlife_news', 'anime_info_cache', 'specialized_news']:  # ★ 追加
                try:
                    t = conn.begin()
                    conn.execute(text(f"SELECT id FROM {tbl} LIMIT 1"))
                    t.commit()
                    logger.info(f"✅ {tbl} テーブル確認OK")
                except Exception:
                    try: t.rollback()
                    except: pass
                    logger.info(f"ℹ️ {tbl} は Base.metadata.create_all で作成されます")

    except Exception as e:
        logger.error(f"⚠️ Migration check failed: {e}")

def fix_postgres_sequences():
    if 'sqlite' in str(DATABASE_URL):
        return

    logger.info("🔧 DBの連番ズレを修正中...")
    # 修正後
    tables = ['user_memories', 'conversation_history', 'user_psychology', 
              'background_tasks', 'holomem_wiki', 'hololive_news', 
              'holomem_nicknames', 'hololive_glossary',
              'stream_reactions', 'holomem_feelings',
              'user_interest_logs', 'friend_profiles',
              'secondlife_news', 'anime_info_cache']
    
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


def setup_stream_processing_schedule():
    """
    配信処理は TASK_SCHEDULE で管理するため、この関数は
    ログ出力のみ（後方互換のために残す）。
    """
    logger.info("✅ 配信処理スケジュール設定完了 (経過時間チェック方式)")
    logger.info("   - process_streams: 24時間ごと")
    logger.info("   - cleanup_streams: 24時間ごと")
    logger.info("   - summarize_feelings: 168時間(週1)ごと")

def initialize_app():
    global engine, Session, groq_client, gemini_model
    logger.info("🔧 初期化開始 (v33.7.0 + セカンドライフ情報 & アニメ自動検索)")
    
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)

        # ★ pgvector拡張の有効化
        try:
            with engine.connect() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.commit()
            logger.info("✅ pgvector拡張 有効化完了")
        except Exception as e:
            logger.warning(f"⚠️ pgvector: {e}")

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
    
    # ★ v33.5.0: タスク関数マップを登録 (catch_up_all_tasks が参照する)
    # ※ 旧来の無条件即時実行 (fetch_hololive_news等) は廃止し、
    #    catch_up_all_tasks() の経過時間チェックに一元化
    _task_func_map.update({
        'fetch_news':           wrapped_news_fetch,
        'update_holomem':       wrapped_holomem_update,
        'update_sns':           update_holomem_social_activities,
        'process_streams':      process_daily_streams,
        'cleanup_streams':      cleanup_old_stream_reactions,
        'summarize_feelings':   summarize_member_feelings,
        'cleanup_interests':    cleanup_old_interest_logs,
        'cleanup_voices':       cleanup_old_voice_files,
        'cleanup_rate_limiter': chat_rate_limiter.cleanup_old_entries,
        # ★ v33.7.0追加
        'fetch_sl_news':        fetch_all_sl_news,
        'cleanup_anime_cache':  cleanup_anime_cache,
    })

    # 起動時キャッチアップ: スリープ中に漏れた全タスクを補完実行
    logger.info("⏰ 起動時キャッチアップ実行中...")
    catch_up_all_tasks()

    # スケジューラスレッド起動 (5分ごとに catch_up_all_tasks を呼ぶ)
    threading.Thread(target=run_scheduler, daemon=True).start()

    setup_stream_processing_schedule()
    cleanup_old_voice_files()

    logger.info("🚀 初期化完了! (v33.7.0)")

initialize_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
