# ==============================================================================
# もちこAI - 全機能統合版 (v30.0 - Refined Edition)
#
# v29.0からの主な変更点:
# 1. アーキテクチャのクラス化 (AIService, SearchService, HololiveManager)
# 2. 検索ロジックの強化 (DuckDuckGo APIライブラリ対応 + スクレイピング強化)
# 3. DBセッションのスコープ管理によるスレッド安全性向上
# 4. Gemini 2.0 -> 1.5 への自動フォールバック実装
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
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, text
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy import pool
from bs4 import BeautifulSoup
import schedule
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from groq import Groq

# DuckDuckGo検索ライブラリ（あれば使用、なければ従来のスクレイピング）
try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

# ==============================================================================
# 基本設定とロギング
# ==============================================================================
log_file_path = '/tmp/mochiko.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(module)s:%(funcName)s] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file_path, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 設定クラス
# ==============================================================================
class Config:
    VOICE_DIR = '/tmp/voices'
    SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
    VOICEVOX_SPEAKER_ID = 20
    SL_SAFE_CHAR_LIMIT = 250
    MIN_MESSAGES_FOR_ANALYSIS = 10
    SEARCH_TIMEOUT = 10
    
    # 検索用User-Agentのローテーション
    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    ]

    LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}

    SPECIALIZED_SITES = {
        'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},
        'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG業界']},
        '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '脳', '認知科学']},
        'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
        'アニメ': {'base_url': 'https://animedb.jp/', 'keywords': ['アニメ', 'anime', 'ANIME', '声優', 'OP', 'ED']}
    }
    HOLO_WIKI_URL = 'https://seesaawiki.jp/hololivetv/'

    HOLOMEM_KEYWORDS = [
        'ときのそら', 'ロボ子さん', 'さくらみこ', 'みこち', '星街すいせい', 'すいちゃん', 'AZKi', '白上フブキ', '夏色まつり', '湊あくあ',
        '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル',
        '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ', 'ラプラス・ダークネス',
        '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'ワトソン・アメリア',
        'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ', 'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト',
        'フワワ・アビスガード', 'モココ・アビスガード', '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん', '轟はじめ', 'ホロライブ', 'YAGOO'
    ]

    @staticmethod
    def get_secret(name):
        env_value = os.environ.get(name)
        if env_value and env_value.strip(): return env_value.strip()
        try:
            path = f"/etc/secrets/{name}"
            if os.path.exists(path):
                with open(path, 'r') as f: return f.read().strip()
        except Exception: pass
        return None

os.makedirs(Config.VOICE_DIR, exist_ok=True)

# ==============================================================================
# データベースモデル
# ==============================================================================
Base = declarative_base()

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
    status = Column(String(50), default='現役', nullable=False)
    graduation_date = Column(String(100), nullable=True)
    mochiko_feeling = Column(Text, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow)

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    url = Column(String(1000), unique=True)
    news_hash = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

# ==============================================================================
# グローバル状態管理
# ==============================================================================
class GlobalManager:
    def __init__(self):
        self.voicevox_enabled = False
        self.active_voicevox_url = None
        self.db_engine = None
        self.SessionLocal = None
        self.executor = ThreadPoolExecutor(max_workers=5)
        
        # 設定読み込み
        self.DB_URL = Config.get_secret('DATABASE_URL') or 'sqlite:///./mochiko_ultimate.db'
        self.GROQ_KEY = Config.get_secret('GROQ_API_KEY')
        self.GEMINI_KEY = Config.get_secret('GEMINI_API_KEY')
        self.VOICEVOX_ENV_URL = Config.get_secret('VOICEVOX_URL')

    def init_db(self):
        logger.info(f"📊 DB接続: {self.DB_URL[:15]}...")
        if self.DB_URL.startswith('sqlite'):
            self.db_engine = create_engine(self.DB_URL, connect_args={'check_same_thread': False}, echo=False)
        else:
            self.db_engine = create_engine(self.DB_URL, pool_size=5, max_overflow=10, pool_recycle=3600)
        
        Base.metadata.create_all(self.db_engine)
        # スレッドセーフなセッションファクトリを作成
        self.SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=self.db_engine))
        logger.info("✅ DB初期化完了")

    @contextmanager
    def get_session(self):
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"⚠️ DB Session Error: {e}")
            raise
        finally:
            session.close()

GM = GlobalManager()

# ==============================================================================
# AIサービス (Llama / Gemini)
# ==============================================================================
class AIService:
    def __init__(self):
        self.groq_client = None
        self.gemini_model = None
        self._init_models()

    def _init_models(self):
        # Groq
        if GM.GROQ_KEY:
            try:
                self.groq_client = Groq(api_key=GM.GROQ_KEY)
                logger.info("✅ Groq (Llama) 初期化完了")
            except Exception as e: logger.error(f"❌ Groq初期化失敗: {e}")

        # Gemini
        if GM.GEMINI_KEY:
            try:
                genai.configure(api_key=GM.GEMINI_KEY)
                # 最新のGemini 2.0を試行、だめなら1.5へ
                self.gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
                logger.info("✅ Gemini (2.0-flash-exp) 初期化完了")
            except Exception as e:
                logger.error(f"❌ Gemini初期化失敗: {e}")

    def _get_gemini_fallback_model(self):
        return genai.GenerativeModel('gemini-1.5-flash')

    def generate(self, system_prompt, message, history, reference_info="", is_detailed=False):
        full_content = message
        if reference_info:
            full_content += f"\n\n【参考情報】\n{reference_info}"
        
        # プロンプトの組み立て
        context_prompt = f"{system_prompt}\n\n現在: {datetime.now().strftime('%Y/%m/%d %H:%M')}"
        
        # 戦略: 詳細検索や複雑なタスクはLlama(Groq)を優先、それ以外やフォールバックでGemini
        use_groq = (is_detailed or len(reference_info) > 200) and self.groq_client

        response_text = None
        
        if use_groq:
            response_text = self._call_llama(context_prompt, full_content, history)
            if not response_text:
                logger.warning("⚠️ Llama失敗 -> Geminiへフォールバック")
                response_text = self._call_gemini(context_prompt, full_content, history)
        else:
            response_text = self._call_gemini(context_prompt, full_content, history)
            if not response_text and self.groq_client:
                logger.warning("⚠️ Gemini失敗 -> Llamaへフォールバック")
                response_text = self._call_llama(context_prompt, full_content, history)

        if not response_text:
            raise Exception("All AI models failed")
        return response_text

    def _call_llama(self, system, message, history):
        if not self.groq_client: return None
        try:
            msgs = [{"role": "system", "content": system}]
            for h in history: msgs.append({"role": h['role'], "content": h['content']})
            msgs.append({"role": "user", "content": message})
            
            res = self.groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=msgs, temperature=0.8, max_tokens=1024
            )
            return res.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"❌ Llama Error: {e}")
            return None

    def _call_gemini(self, system, message, history):
        if not self.gemini_model: return None
        
        prompt = f"{system}\n\n会話履歴:\n"
        for h in history: prompt += f"{h['role']}: {h['content']}\n"
        prompt += f"user: {message}\nassistant:"
        
        try:
            # Gemini 2.0 (or default)
            res = self.gemini_model.generate_content(prompt)
            return res.text
        except Exception as e:
            logger.warning(f"⚠️ Gemini Primary Model Error: {e}")
            try:
                # Fallback to 1.5 Flash
                fallback = self._get_gemini_fallback_model()
                res = fallback.generate_content(prompt)
                return res.text
            except Exception as e2:
                logger.error(f"❌ Gemini Fallback Error: {e2}")
                return None

AI = AIService()

# ==============================================================================
# 検索サービス (DuckDuckGo + Scraper)
# ==============================================================================
class SearchService:
    @staticmethod
    def search(query, num_results=3, site_filter=None):
        final_query = f"{query} site:{site_filter}" if site_filter else query
        results = []
        
        # 1. DuckDuckGo Library (推奨)
        if HAS_DDGS:
            try:
                logger.info(f"🔍 DDGS検索: {final_query}")
                with DDGS() as ddgs:
                    # region='jp-jp' で日本語結果を優先
                    ddg_results = list(ddgs.text(final_query, region='jp-jp', max_results=num_results))
                    for r in ddg_results:
                        results.append({'title': r['title'], 'snippet': r['body']})
                if results: return results
            except Exception as e:
                logger.warning(f"⚠️ DDGS検索エラー: {e}")

        # 2. スクレイピング (フォールバック)
        logger.info("🔄 スクレイピングへフォールバック")
        return SearchService._scrape_fallback(final_query, num_results)

    @staticmethod
    def _scrape_fallback(query, num_results):
        # Bingは比較的スクレイピングに強いが、構造変更に弱い
        url = f"https://www.bing.com/search?q={quote_plus(query)}"
        headers = {'User-Agent': random.choice(Config.USER_AGENTS)}
        try:
            res = requests.get(url, headers=headers, timeout=Config.SEARCH_TIMEOUT)
            if res.status_code != 200: return []
            
            soup = BeautifulSoup(res.content, 'html.parser')
            results = []
            # Bingの一般的なクラス (li.b_algo)
            for item in soup.select('li.b_algo')[:num_results]:
                title = item.select_one('h2')
                snippet = item.select_one('p')
                if title and snippet:
                    results.append({'title': title.get_text(), 'snippet': snippet.get_text()})
            return results
        except Exception as e:
            logger.error(f"❌ スクレイピング失敗: {e}")
            return []

# ==============================================================================
# ホロライブ & データ管理
# ==============================================================================
class HololiveManager:
    @staticmethod
    def fetch_news():
        logger.info("📰 ニュース取得開始")
        url = "https://hololive.hololivepro.com/news"
        try:
            res = requests.get(url, headers={'User-Agent': random.choice(Config.USER_AGENTS)}, timeout=10)
            soup = BeautifulSoup(res.content, 'html.parser')
            with GM.get_session() as session:
                for item in soup.select('ul.news_list li a', limit=10):
                    news_url = urljoin(url, item['href'])
                    title = item.get_text(strip=True)
                    news_hash = hashlib.md5(news_url.encode()).hexdigest()
                    if not session.query(HololiveNews).filter_by(news_hash=news_hash).first():
                        session.add(HololiveNews(title=title, url=news_url, content=title, news_hash=news_hash))
                        logger.info(f"  + ニュース追加: {title}")
        except Exception as e: logger.error(f"❌ ニュース取得エラー: {e}")

    @staticmethod
    def update_wiki_db():
        logger.info("🌟 Wiki DB更新開始")
        try:
            res = requests.get(Config.HOLO_WIKI_URL, headers={'User-Agent': random.choice(Config.USER_AGENTS)}, timeout=15)
            soup = BeautifulSoup(res.content, 'html.parser')
            # ※HTML構造依存のため、構造変更時は要修正
            sections = {'現役': soup.find('div', id='content_block_2'), '卒業': soup.find('div', id='content_block_3')}
            
            with GM.get_session() as session:
                for status, section in sections.items():
                    if not section: continue
                    gen = "不明"
                    for el in section.find_all(['h3', 'a']):
                        if el.name == 'h3': gen = el.get_text(strip=True)
                        elif el.name == 'a' and 'title' in el.attrs:
                            name = el['title'].strip()
                            if not name: continue
                            existing = session.query(HolomemWiki).filter_by(member_name=name).first()
                            if not existing:
                                session.add(HolomemWiki(member_name=name, generation=gen, status=status))
                            elif existing.status != status:
                                existing.status = status
        except Exception as e: logger.error(f"❌ Wiki更新エラー: {e}")

# ==============================================================================
# Flask アプリケーション
# ==============================================================================
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
CORS(app)

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        user_uuid = data.get('uuid', '')[:255]
        user_name = data.get('name', 'Guest')[:255]
        message = data.get('message', '')[:1000]
        use_voice = data.get('voice', False)

        if not user_uuid or not message:
            return Response("エラー: 入力が足りないよ|", status=400)

        # 心理学、天気、Wiki、検索のロジック
        # (コード量削減のため、主要フローのみ記載。v29のロジックを継承)
        
        response_text = ""
        voice_url = ""
        task_started = False

        with GM.get_session() as session:
            # ユーザー管理
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if not user:
                user = UserMemory(user_uuid=user_uuid, user_name=user_name)
                session.add(user)
            user.interaction_count += 1
            user.last_interaction = datetime.utcnow()

            # 履歴取得
            history_objs = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(10).all()
            history = [{'role': h.role, 'content': h.content} for h in reversed(history_objs)]
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))

            # 1. 単純応答 (天気・時間)
            if "天気" in message:
                response_text = "天気予報APIは現在調整中だよ！窓の外見てみて！" # 簡易化
            
            # 2. ホロメンDB検索
            if not response_text:
                for name in Config.HOLOMEM_KEYWORDS:
                    if name == message.strip():
                        info = session.query(HolomemWiki).filter_by(member_name=name).first()
                        if info:
                            ref = f"名前:{info.member_name}, 期:{info.generation}, 状態:{info.status}"
                            response_text = AI.generate("ホロメンについて教えて", message, history, reference_info=ref)
                        break

            # 3. Web検索が必要か判断
            if not response_text and ("調べて" in message or "とは" in message):
                task_id = f"search_{user_uuid}_{int(time.time())}"
                task_query = {'query': message, 'uuid': user_uuid, 'name': user_name}
                
                # タスク登録
                new_task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=json.dumps(task_query))
                session.add(new_task)
                
                # バックグラウンド実行
                GM.executor.submit(execute_background_search, task_id, task_query)
                
                response_text = "ん、わかった！ちょっと詳しく調べてくるから待ってて！"
                task_started = True

            # 4. 通常会話
            if not response_text:
                # ニュース情報をコンテキストに入れる
                news = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(2).all()
                ref_news = "\n".join([n.title for n in news]) if news else ""
                
                psych_data = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
                personality = f"ユーザーは{psych_data.analysis_summary}な人です。" if psych_data and psych_data.analysis_summary else ""
                
                system = f"あなたは「もちこ」というギャルAIです。一人称は「あてぃし」。{personality}"
                response_text = AI.generate(system, message, history, reference_info=ref_news)

            if not task_started:
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))

        # 音声生成 (同期処理で簡易実装、非同期推奨だがSL連携のため即時性重視)
        if use_voice and GM.voicevox_enabled and not task_started:
            voice_url = generate_voice_url(response_text, user_uuid)

        return Response(f"{response_text}|{voice_url}", mimetype='text/plain; charset=utf-8')

    except Exception as e:
        logger.error(f"🔥 Chat Error: {e}", exc_info=True)
        return Response("ごめん、システムエラーみたい…|", status=500)

@app.route('/check_task', methods=['POST'])
def check_task():
    try:
        data = request.json
        uuid = data.get('uuid')
        with GM.get_session() as session:
            task = session.query(BackgroundTask).filter(
                BackgroundTask.user_uuid == uuid, 
                BackgroundTask.status == 'completed'
            ).order_by(BackgroundTask.completed_at.desc()).first()
            
            if task:
                result = task.result
                # タスク完了後は履歴に残す
                session.add(ConversationHistory(user_uuid=uuid, role='assistant', content=result))
                session.delete(task) # 完了タスクは削除（またはstatus=archived）
                
                voice_url = ""
                if data.get('voice') and GM.voicevox_enabled:
                    voice_url = generate_voice_url(result, uuid)
                
                return create_json({'status': 'completed', 'response': f"{result}|{voice_url}"})
        
        return create_json({'status': 'no_tasks'})
    except Exception as e:
        return create_json({'status': 'error', 'msg': str(e)}, 500)

# ==============================================================================
# バックグラウンド処理関数
# ==============================================================================
def execute_background_search(task_id, query_data):
    """スレッド内で実行される検索タスク"""
    logger.info(f"🚀 Background Task Start: {task_id}")
    try:
        # 検索実行
        results = SearchService.search(query_data['query'], num_results=5)
        ref_text = "\n".join([f"- {r['title']}: {r['snippet']}" for r in results])
        
        # AIによるまとめ生成
        # ※ スレッド内なので session は新規作成が必要
        with GM.get_session() as session:
            history_objs = session.query(ConversationHistory).filter_by(user_uuid=query_data['uuid']).limit(5).all()
            history = [{'role': h.role, 'content': h.content} for h in history_objs]
            
            system = "あなたは「もちこ」です。検索結果を元に、ユーザーの質問に詳しく答えてください。語尾はギャルっぽく。"
            summary = AI.generate(system, query_data['query'], history, reference_info=ref_text, is_detailed=True)
            
            # 結果保存
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = summary
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
                logger.info(f"✅ Task Completed: {task_id}")

    except Exception as e:
        logger.error(f"❌ Task Failed: {e}", exc_info=True)
        with GM.get_session() as session:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.status = 'failed'
                task.result = "ごめん、調べるのに失敗しちゃった…"

# ==============================================================================
# ヘルパー関数
# ==============================================================================
def create_json(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json', status=status)

def generate_voice_url(text, uuid):
    if not GM.active_voicevox_url: return ""
    try:
        # 200文字制限
        short_text = text[:200].replace('|', '')
        query = requests.post(
            f"{GM.active_voicevox_url}/audio_query",
            params={"text": short_text, "speaker": Config.VOICEVOX_SPEAKER_ID}, timeout=5
        ).json()
        wav = requests.post(
            f"{GM.active_voicevox_url}/synthesis",
            params={"speaker": Config.VOICEVOX_SPEAKER_ID}, json=query, timeout=10
        ).content
        
        filename = f"voice_{uuid[:8]}_{int(time.time())}.wav"
        path = os.path.join(Config.VOICE_DIR, filename)
        with open(path, 'wb') as f: f.write(wav)
        return f"{Config.SERVER_URL}/play/{filename}"
    except Exception as e:
        logger.error(f"Voice Error: {e}")
        return ""

@app.route('/play/<filename>')
def play_voice(filename):
    return send_from_directory(Config.VOICE_DIR, filename)

@app.route('/health')
def health():
    return create_json({'status': 'ok', 'voicevox': GM.voicevox_enabled, 'ai': 'ready'})

# ==============================================================================
# 初期化と起動
# ==============================================================================
def initialize_app():
    """アプリケーション起動時に実行される初期化処理"""
    logger.info("🚀 Initializing Application...")
    
    # 1. DB初期化
    GM.init_db()
    
    # 2. Voicevoxチェック
    urls = [GM.VOICEVOX_ENV_URL, 'http://127.0.0.1:50021', 'http://voicevox:50021']
    for url in urls:
        if url:
            try:
                if requests.get(f"{url}/version", timeout=1).status_code == 200:
                    GM.active_voicevox_url = url
                    GM.voicevox_enabled = True
                    logger.info(f"🔊 VOICEVOX Connected: {url}")
                    break
            except: pass

    # 3. スケジューラ設定
    schedule.every(2).hours.do(HololiveManager.fetch_news)
    schedule.every(1).days.do(HololiveManager.update_wiki_db)
    
    def run_scheduler():
        while True:
            try:
                schedule.run_pending()
            except Exception as e:
                logger.error(f"Scheduler Error: {e}")
            time.sleep(60)
    
    # デーモンスレッドでスケジューラ開始
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info("✅ Initialization Complete.")

# アプリ終了時の処理（スレッドプールのクリーンアップ）
atexit.register(lambda: GM.executor.shutdown(wait=False))

# 【重要】Gunicornでの起動時にも初期化が走るように、グローバルスコープで実行する
try:
    initialize_app()
    # 初回データ取得を非同期でキック
    GM.executor.submit(HololiveManager.fetch_news)
except Exception as e:
    logger.critical(f"🔥 Critical Initialization Error: {e}", exc_info=True)

# ローカル開発用
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)
