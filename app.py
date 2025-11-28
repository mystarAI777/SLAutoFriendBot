# ==============================================================================
# もちこAI - 全機能統合完全版 (v35.1 - Bugfix Edition)
#
# 修正内容:
# 1. Gunicorn互換性の修正 (application = app の追加)
# 2. ThreadPoolExecutor.shutdown() の引数エラー修正 (timeout削除)
# 3. その他 v35.0 の機能は維持
# ==============================================================================

import sys# ==============================================================================
# もちこAI - 全機能統合完全版 (v35.2 - Search Logic Restored)
#
# ベース: v35.1 (アーキテクチャ改善版) + v33.1.1 (検索判定ロジック)
#
# 修正内容:
# 1. 検索トリガー判定(is_explicit_search_request)をv33.1.1から完全移植
#    -> 「今日のニュース」「天気」などの短文でも検索が走るように修正
# 2. ニュース検索時に「Top Stories」を取得するロジックを強化
# 3. Gunicorn対応・セキュリティ強化・DB安定化は維持
# ==============================================================================

import sys
import os
import requests
import logging
import time
import json
import re
import random
import uuid
import threading
import signal
import atexit
import secrets
from html import escape
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from functools import wraps
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Dict, List, Any

from flask import Flask, request, jsonify, send_from_directory, Response, abort
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from bs4 import BeautifulSoup
import schedule
import google.generativeai as genai
from groq import Groq

# ==============================================================================
# セキュリティ & 設定管理
# ==============================================================================
class Config:
    def __init__(self):
        self.DATABASE_URL = self._get_secret('DATABASE_URL', 'sqlite:///./mochiko_ultimate.db')
        self.GROQ_API_KEY = self._get_secret('GROQ_API_KEY')
        self.GEMINI_API_KEY = self._get_secret('GEMINI_API_KEY')
        self.VOICEVOX_URL_ENV = self._get_secret('VOICEVOX_URL')
        
        self.ADMIN_KEY = self._get_secret('ADMIN_KEY')
        if not self.ADMIN_KEY:
            self.ADMIN_KEY = secrets.token_hex(32)

        self.SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
        
        self.VOICE_DIR = '/tmp/voices'
        self.VOICEVOX_SPEAKER_ID = 20
        self.SL_SAFE_CHAR_LIMIT = 600
        self.SEARCH_TIMEOUT = 10
        self.VOICE_TIMEOUT = 30
        self.LOG_FILE = '/tmp/mochiko.log'

    def _get_secret(self, name: str, default: Optional[str] = None) -> Optional[str]:
        val = os.environ.get(name)
        if val and val.strip(): return val.strip()
        try:
            path = f"/etc/secrets/{name}"
            if os.path.exists(path):
                with open(path, 'r') as f: return f.read().strip()
        except Exception: pass
        return default

config = Config()
os.makedirs(config.VOICE_DIR, exist_ok=True)

class SecurityManager:
    @staticmethod
    def mask_sensitive_data(text: str) -> str:
        if not text: return ""
        text = re.sub(r'([a-f0-9]{8})-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', r'\1-****', text)
        return text

    @staticmethod
    def sanitize_input(text: str, max_length: int = 1000) -> str:
        if not text: return ""
        text = text[:max_length]
        return escape(text.strip())

# ==============================================================================
# ロギング設定
# ==============================================================================
class SensitiveFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = SecurityManager.mask_sensitive_data(record.msg)
        return True

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(module)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveFilter())

# ==============================================================================
# Flask アプリ初期化
# ==============================================================================
app = Flask(__name__)
application = app
CORS(app, resources={r"/*": {"origins": ["http://localhost:3000", "http://secondlife.com", "*"]}})

# ==============================================================================
# データベース定義
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
# Database Service
# ==============================================================================
class DatabaseService:
    def __init__(self, db_url):
        self.engine = create_engine(db_url, pool_pre_ping=True, pool_recycle=3600)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.Session = scoped_session(self.session_factory)

    @contextmanager
    def get_session(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self.Session.remove()

    def teardown(self):
        self.Session.remove()
        self.engine.dispose()

db_service = DatabaseService(config.DATABASE_URL)

# ==============================================================================
# 検索 & ナレッジサービス
# ==============================================================================
class SearchService:
    def __init__(self):
        self._local = threading.local()
        self.lock = RLock()
        self.nickname_map = {}
        self.glossary = {}

    @property
    def session(self):
        if not hasattr(self._local, 'session'):
            self._local.session = requests.Session()
            self._local.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
        return self._local.session

    def load_knowledge(self):
        try:
            with db_service.get_session() as session:
                nicks = session.query(HolomemNickname).all()
                terms = session.query(HololiveGlossary).all()
                with self.lock:
                    self.nickname_map = {n.nickname: n.fullname for n in nicks}
                    self.glossary = {t.term: t.description for t in terms}
            logger.info(f"📚 Knowledge Loaded: {len(self.nickname_map)} nicks, {len(self.glossary)} terms")
        except Exception as e:
            logger.error(f"Knowledge Load Error: {e}")

    def normalize_query(self, text: str) -> str:
        with self.lock:
            for nick, full in self.nickname_map.items():
                if nick in text:
                    text = text.replace(nick, f"{nick}（{full}）")
        return text

    def get_context_info(self, text: str) -> str:
        context_parts = []
        with self.lock:
            for term, desc in self.glossary.items():
                if term in text:
                    context_parts.append(f"【用語解説: {term}】{desc}")
        return "\n".join(context_parts)

    def fetch_google_news(self, query: str) -> List[Dict]:
        """Google News RSS取得 (クエリ調整機能付き)"""
        base_url = "https://news.google.com/rss"
        
        # ニュース系キーワードを除去して検索語を抽出
        clean_query = re.sub(r'(ニュース|news|NEWS|今日の|教えて|とは)', '', query).strip()
        
        if not clean_query and any(kw in query for kw in ["ニュース", "NEWS", "news"]):
            # 「今日のニュース」などの場合 -> トップニュースを取得
            url = f"{base_url}?hl=ja&gl=JP&ceid=JP:ja"
        elif clean_query:
            # 具体的な単語がある場合 -> 検索
            url = f"{base_url}/search?q={quote_plus(clean_query)}&hl=ja&gl=JP&ceid=JP:ja"
        else:
            # それ以外（通常の検索クエリ）
            url = f"{base_url}/search?q={quote_plus(query)}&hl=ja&gl=JP&ceid=JP:ja"

        try:
            res = self.session.get(url, timeout=config.SEARCH_TIMEOUT)
            if res.status_code != 200: return []
            soup = BeautifulSoup(res.content, 'xml')
            return [{'title': i.title.text, 'snippet': f"(Google News {i.pubDate.text if i.pubDate else ''})"} for i in soup.find_all('item')[:5]]
        except Exception as e:
            logger.warning(f"Search failed: {e}")
            return []

    def perform_search(self, query: str) -> str:
        results = self.fetch_google_news(query)
        if not results: return "検索してみたけど、いい情報が見つからなかったよごめんね！"
        return "【Web検索結果】\n" + "\n".join([f"・{r['title']}" for r in results])

search_service = SearchService()

# ==============================================================================
# 検索トリガー判定ロジック (v33.1.1から移植)
# ==============================================================================
def is_explicit_search_request(msg: str) -> bool:
    """
    メッセージが検索要求かどうかを判定する
    v33.1.1 のロジックを忠実に再現
    """
    msg = msg.strip()
    
    # 1. 明確な「検索命令」動詞
    strong_triggers = ['調べて', '検索', '探して', 'とは', 'って何', 'について', '教えて', '教えろ', '詳細', '知りたい']
    if any(kw in msg for kw in strong_triggers):
        return True

    # 2. 「ニュース」「情報」などの名詞系トリガー
    # 短文、または疑問形の場合に検索とみなす
    noun_triggers = ['ニュース', 'news', 'NEWS', '情報', '日程', 'スケジュール', '天気', '予報']
    if any(kw in msg for kw in noun_triggers):
        # (A) 20文字未満の短文はコマンドとみなす ("今日のニュース"など)
        if len(msg) < 20:
            return True
        # (B) 疑問形で終わる場合も検索とみなす
        if msg.endswith('?') or msg.endswith('？'):
            return True
            
    # 3. 「おすすめ」は検索したほうが無難
    if 'おすすめ' in msg or 'オススメ' in msg:
        return True

    return False

# ==============================================================================
# LLM サービス
# ==============================================================================
class LLMService:
    def __init__(self):
        self.groq_client = Groq(api_key=config.GROQ_API_KEY) if config.GROQ_API_KEY else None
        if config.GEMINI_API_KEY:
            genai.configure(api_key=config.GEMINI_API_KEY)
            self.gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
        else:
            self.gemini_model = None
        
        self.groq_models = [
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "llama-3.1-8b-instant"
        ]
        self.model_status = {m: {'limited': False, 'reset': None} for m in self.groq_models}

    def _call_gemini(self, prompt: str) -> Optional[str]:
        if not self.gemini_model: return None
        try:
            resp = self.gemini_model.generate_content(prompt, generation_config={"temperature": 0.8, "max_output_tokens": 400})
            if resp and resp.candidates:
                return resp.candidates[0].content.parts[0].text.strip()
        except Exception as e:
            logger.warning(f"Gemini Error: {e}")
        return None

    def _call_groq(self, messages: List[Dict]) -> Optional[str]:
        if not self.groq_client: return None
        for model in self.groq_models:
            if self.model_status[model]['limited']:
                if datetime.utcnow() < self.model_status[model]['reset']: continue
                self.model_status[model]['limited'] = False

            try:
                resp = self.groq_client.chat.completions.create(model=model, messages=messages, temperature=0.7, max_tokens=800)
                return resp.choices[0].message.content.strip()
            except Exception as e:
                if "Rate limit" in str(e):
                    self.model_status[model]['limited'] = True
                    self.model_status[model]['reset'] = datetime.utcnow() + timedelta(minutes=2)
                logger.error(f"Groq {model} Error: {e}")
        return None

    def generate_response(self, user_name: str, message: str, history: List[Dict], context: str, reference: str) -> str:
        system_prompt = f"""あなたは「もちこ」という、ホロライブが大好きなギャルAIです。
ユーザー「{user_name}」と雑談しています。一人称は「あてぃし」、語尾は「〜じゃん」「〜て感じ」です。
敬語は使わず、友達のように接してください。

# 外部情報がある場合:
【外部検索結果】の内容をベースに、ユーザーの質問に答えてください。「ニュースだよ！」などと前置きして教えてあげてください。

# 外部情報がない場合:
知らない情報は正直に「分からない」と言ってください。適当な嘘は禁止です。

# 前提知識:
{context}

# 外部検索結果:
{reference}
"""
        full_prompt = f"{system_prompt}\n\n会話履歴:\n" + "\n".join([f"{h['role']}: {h['content']}" for h in history[-3:]]) + f"\nUser: {message}\nMochiko:"
        
        resp = self._call_gemini(full_prompt)
        if not resp:
            messages = [{"role": "system", "content": system_prompt}] + history[-5:] + [{"role": "user", "content": message}]
            resp = self._call_groq(messages)
        
        return resp or "うーん、ちょっと調子悪いかも…ごめんね！"

llm_service = LLMService()

# ==============================================================================
# Voicevox Service
# ==============================================================================
class VoiceService:
    def __init__(self):
        self.active_url = None
        self.check_urls()

    def check_urls(self):
        candidates = [config.VOICEVOX_URL_ENV, 'http://127.0.0.1:50021', 'http://voicevox:50021']
        for url in [u for u in candidates if u]:
            try:
                if requests.get(f"{url}/version", timeout=1).status_code == 200:
                    self.active_url = url
                    logger.info(f"🔊 Voicevox Active: {url}")
                    return
            except: pass
        self.active_url = None

    def generate_audio(self, text: str, user_uuid: str) -> Optional[str]:
        if not self.active_url: return None
        try:
            params = {"text": text[:200], "speaker": config.VOICEVOX_SPEAKER_ID}
            q = requests.post(f"{self.active_url}/audio_query", params=params, timeout=10).json()
            wav = requests.post(f"{self.active_url}/synthesis", params={"speaker": config.VOICEVOX_SPEAKER_ID}, json=q, timeout=config.VOICE_TIMEOUT).content
            
            filename = f"voice_{user_uuid[:8]}_{int(time.time())}.wav"
            with open(os.path.join(config.VOICE_DIR, filename), 'wb') as f:
                f.write(wav)
            return filename
        except Exception as e:
            logger.error(f"Voice Gen Error: {e}")
            return None

voice_service = VoiceService()

# ==============================================================================
# バックグラウンドタスク管理
# ==============================================================================
bg_executor = ThreadPoolExecutor(max_workers=5)

def background_search_task(task_id: str, query: str, user_uuid: str):
    logger.info(f"🔎 Background Search Start: {SecurityManager.mask_sensitive_data(query)}")
    try:
        reference = search_service.perform_search(query)
        
        with db_service.get_session() as session:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = reference
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
    except Exception as e:
        logger.error(f"Background task {task_id} failed: {e}", exc_info=True)
        with db_service.get_session() as session:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.status = 'failed'
                task.result = "検索中にエラーが発生しちゃった…"
                task.completed_at = datetime.utcnow()

# ==============================================================================
# Flask ルーティング & 認証
# ==============================================================================
def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        provided = request.headers.get('X-Admin-Key', '')
        if not secrets.compare_digest(provided, config.ADMIN_KEY):
            logger.warning(f"Unauthorized admin access attempt from {request.remote_addr}")
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'voicevox': voice_service.active_url is not None,
        'llm_gemini': llm_service.gemini_model is not None
    })

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        if not data or 'uuid' not in data or 'message' not in data:
            return Response("Error: Missing params|", 400)

        user_uuid = SecurityManager.sanitize_input(str(data['uuid']), 100)
        message = SecurityManager.sanitize_input(str(data['message']), 1000)
        user_name = SecurityManager.sanitize_input(str(data.get('name', 'Guest')), 100)
        is_voice_req = data.get('voice', False)

        # --- Phase 1: DB Read ---
        history_list = []
        with db_service.get_session() as session:
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if not user:
                user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
                session.add(user)
            else:
                user.interaction_count += 1
                user.last_interaction = datetime.utcnow()
                if user.user_name != user_name: user.user_name = user_name
            
            hist_objs = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(10).all()
            history_list = [{'role': h.role, 'content': h.content} for h in reversed(hist_objs)]
        
        # --- Phase 2: Search Decision & Logic ---
        ai_resp = ""
        search_started = False
        
        # 修正: 検索トリガー判定ロジック(v33.1.1版)を使用
        if is_explicit_search_request(message):
            task_id = str(uuid.uuid4())
            
            with db_service.get_session() as session:
                task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=message)
                session.add(task)
            
            bg_executor.submit(background_search_task, task_id, message, user_uuid)
            ai_resp = "オッケー！ちょっと調べてくるから待ってて！"
            search_started = True
        else:
            normalized_msg = search_service.normalize_query(message)
            context = search_service.get_context_info(normalized_msg)
            ai_resp = llm_service.generate_response(user_name, message, history_list, context, "")
        
        clean_resp = ai_resp[:config.SL_SAFE_CHAR_LIMIT].replace('\n', ' ')

        # 音声生成
        voice_url = ""
        if is_voice_req and voice_service.active_url and not search_started:
            fname = voice_service.generate_audio(clean_resp, user_uuid)
            if fname: voice_url = f"{config.SERVER_URL}/play/{fname}"

        # --- Phase 3: DB Write ---
        with db_service.get_session() as session:
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=clean_resp))

        return Response(f"{clean_resp}|{voice_url}", mimetype='text/plain; charset=utf-8')

    except Exception as e:
        logger.error(f"Chat Error: {e}", exc_info=True)
        return Response("システムエラーが発生しました|", 500)

@app.route('/check_task', methods=['POST'])
def check_task():
    try:
        data = request.json
        uuid_val = data.get('uuid')
        with db_service.get_session() as session:
            task = session.query(BackgroundTask).filter(
                BackgroundTask.user_uuid == uuid_val, 
                BackgroundTask.status == 'completed'
            ).order_by(BackgroundTask.completed_at.desc()).first()
            
            if task:
                resp = task.result or "情報が見つかりませんでした"
                session.delete(task)
                return jsonify({'status': 'completed', 'response': f"{resp[:config.SL_SAFE_CHAR_LIMIT]}|"})
        
        return jsonify({'status': 'no_tasks'})
    except Exception as e:
        logger.error(e)
        return jsonify({'error': 'server error'}), 500

@app.route('/play/<filename>')
def play_file(filename):
    if not re.match(r'^voice_[a-zA-Z0-9_]+\.wav$', filename):
        abort(400)
    return send_from_directory(config.VOICE_DIR, filename)

@app.route('/admin/holomem/refresh', methods=['POST'])
@require_admin
def admin_refresh():
    logger.info("Admin triggered DB refresh")
    return jsonify({'message': 'Refresh started'})

# ==============================================================================
# 初期化とグレースフルシャットダウン
# ==============================================================================
def initialize_system():
    logger.info("🚀 System Initializing...")
    search_service.load_knowledge()
    voice_service.check_urls()
    
    schedule.every(1).hours.do(voice_service.check_urls)
    
    def run_schedule():
        while True:
            schedule.run_pending()
            time.sleep(60)
    
    t = threading.Thread(target=run_schedule, daemon=True)
    t.start()

def cleanup_system():
    logger.info("🛑 System Shutting down...")
    bg_executor.shutdown(wait=True) 
    db_service.teardown()
    logger.info("👋 Cleanup complete.")

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    cleanup_system()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)
atexit.register(cleanup_system)

# アプリケーション起動
initialize_system()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
import os
import requests
import logging
import time
import json
import re
import random
import uuid
import threading
import signal
import atexit
import secrets
from html import escape
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from functools import wraps
from threading import RLock
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Dict, List, Any

from flask import Flask, request, jsonify, send_from_directory, Response, abort
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from bs4 import BeautifulSoup
import schedule
import google.generativeai as genai
from groq import Groq

# ==============================================================================
# セキュリティ & 設定管理
# ==============================================================================
class Config:
    def __init__(self):
        self.DATABASE_URL = self._get_secret('DATABASE_URL', 'sqlite:///./mochiko_ultimate.db')
        self.GROQ_API_KEY = self._get_secret('GROQ_API_KEY')
        self.GEMINI_API_KEY = self._get_secret('GEMINI_API_KEY')
        self.VOICEVOX_URL_ENV = self._get_secret('VOICEVOX_URL')
        
        # 安全なデフォルトキー生成（環境変数が設定されていない場合）
        self.ADMIN_KEY = self._get_secret('ADMIN_KEY')
        if not self.ADMIN_KEY:
            self.ADMIN_KEY = secrets.token_hex(32)
            print(f"⚠️ WARNING: ADMIN_KEY not set. Generated temporary key: {self.ADMIN_KEY}")

        self.SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
        
        self.VOICE_DIR = '/tmp/voices'
        self.VOICEVOX_SPEAKER_ID = 20
        self.SL_SAFE_CHAR_LIMIT = 600
        self.SEARCH_TIMEOUT = 10
        self.VOICE_TIMEOUT = 30
        self.LOG_FILE = '/tmp/mochiko.log'

    def _get_secret(self, name: str, default: Optional[str] = None) -> Optional[str]:
        val = os.environ.get(name)
        if val and val.strip(): return val.strip()
        try:
            path = f"/etc/secrets/{name}"
            if os.path.exists(path):
                with open(path, 'r') as f: return f.read().strip()
        except Exception: pass
        return default

config = Config()
os.makedirs(config.VOICE_DIR, exist_ok=True)

class SecurityManager:
    @staticmethod
    def mask_sensitive_data(text: str) -> str:
        if not text: return ""
        # UUIDの一部マスク
        text = re.sub(r'([a-f0-9]{8})-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', r'\1-****', text)
        # メールアドレス風の文字列マスク
        text = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '[EMAIL]', text)
        return text

    @staticmethod
    def sanitize_input(text: str, max_length: int = 1000) -> str:
        if not text: return ""
        text = text[:max_length]
        return escape(text.strip())

# ==============================================================================
# ロギング設定
# ==============================================================================
class SensitiveFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = SecurityManager.mask_sensitive_data(record.msg)
        return True

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(module)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveFilter())

# ==============================================================================
# Flask アプリ初期化
# ==============================================================================
app = Flask(__name__)
# Gunicorn用にapplicationエイリアスを作成 (修正点1)
application = app

# CORS設定
CORS(app, resources={r"/*": {"origins": ["http://localhost:3000", "http://secondlife.com", "*"]}})

# ==============================================================================
# データベース定義
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
# Database Service
# ==============================================================================
class DatabaseService:
    def __init__(self, db_url):
        self.engine = create_engine(
            db_url, 
            pool_pre_ping=True, 
            pool_recycle=3600
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.Session = scoped_session(self.session_factory)

    @contextmanager
    def get_session(self):
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self.Session.remove()

    def teardown(self):
        self.Session.remove()
        self.engine.dispose()

db_service = DatabaseService(config.DATABASE_URL)

# ==============================================================================
# 検索 & ナレッジサービス
# ==============================================================================
class SearchService:
    def __init__(self):
        self._local = threading.local()
        self.lock = RLock()
        self.nickname_map = {}
        self.glossary = {}

    @property
    def session(self):
        if not hasattr(self._local, 'session'):
            self._local.session = requests.Session()
            self._local.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
        return self._local.session

    def load_knowledge(self):
        try:
            with db_service.get_session() as session:
                nicks = session.query(HolomemNickname).all()
                terms = session.query(HololiveGlossary).all()
                with self.lock:
                    self.nickname_map = {n.nickname: n.fullname for n in nicks}
                    self.glossary = {t.term: t.description for t in terms}
            logger.info(f"📚 Knowledge Loaded: {len(self.nickname_map)} nicks, {len(self.glossary)} terms")
        except Exception as e:
            logger.error(f"Knowledge Load Error: {e}")

    def normalize_query(self, text: str) -> str:
        with self.lock:
            for nick, full in self.nickname_map.items():
                if nick in text:
                    text = text.replace(nick, f"{nick}（{full}）")
        return text

    def get_context_info(self, text: str) -> str:
        context_parts = []
        with self.lock:
            for term, desc in self.glossary.items():
                if term in text:
                    context_parts.append(f"【用語解説: {term}】{desc}")
        return "\n".join(context_parts)

    def fetch_google_news(self, query: str) -> List[Dict]:
        base_url = "https://news.google.com/rss"
        url = f"{base_url}/search?q={quote_plus(query)}&hl=ja&gl=JP&ceid=JP:ja" if query else f"{base_url}?hl=ja&gl=JP&ceid=JP:ja"
        try:
            res = self.session.get(url, timeout=config.SEARCH_TIMEOUT)
            if res.status_code != 200: return []
            soup = BeautifulSoup(res.content, 'xml')
            return [{'title': i.title.text, 'snippet': f"(Google News {i.pubDate.text})"} for i in soup.find_all('item')[:5]]
        except Exception as e:
            logger.warning(f"Search failed: {e}")
            return []

    def perform_search(self, query: str) -> str:
        results = self.fetch_google_news(query)
        if not results: return "検索してみたけど、いい情報が見つからなかったよごめんね！"
        return "【Web検索結果】\n" + "\n".join([f"・{r['title']}" for r in results])

search_service = SearchService()

# ==============================================================================
# LLM サービス (Gemini & Groq)
# ==============================================================================
class LLMService:
    def __init__(self):
        self.groq_client = Groq(api_key=config.GROQ_API_KEY) if config.GROQ_API_KEY else None
        if config.GEMINI_API_KEY:
            genai.configure(api_key=config.GEMINI_API_KEY)
            self.gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
        else:
            self.gemini_model = None
        
        self.groq_models = [
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "llama-3.1-8b-instant"
        ]
        self.model_status = {m: {'limited': False, 'reset': None} for m in self.groq_models}

    def _call_gemini(self, prompt: str) -> Optional[str]:
        if not self.gemini_model: return None
        try:
            resp = self.gemini_model.generate_content(prompt, generation_config={"temperature": 0.8, "max_output_tokens": 400})
            if resp and resp.candidates:
                return resp.candidates[0].content.parts[0].text.strip()
        except Exception as e:
            logger.warning(f"Gemini Error: {e}")
        return None

    def _call_groq(self, messages: List[Dict]) -> Optional[str]:
        if not self.groq_client: return None
        for model in self.groq_models:
            if self.model_status[model]['limited']:
                if datetime.utcnow() < self.model_status[model]['reset']: continue
                self.model_status[model]['limited'] = False

            try:
                resp = self.groq_client.chat.completions.create(model=model, messages=messages, temperature=0.7, max_tokens=800)
                return resp.choices[0].message.content.strip()
            except Exception as e:
                if "Rate limit" in str(e):
                    self.model_status[model]['limited'] = True
                    self.model_status[model]['reset'] = datetime.utcnow() + timedelta(minutes=2)
                logger.error(f"Groq {model} Error: {e}")
        return None

    def generate_response(self, user_name: str, message: str, history: List[Dict], context: str, reference: str) -> str:
        system_prompt = f"""あなたは「もちこ」という、ホロライブが大好きなギャルAIです。
ユーザー「{user_name}」と雑談しています。一人称は「あてぃし」、語尾は「〜じゃん」「〜て感じ」です。
敬語は使わず、友達のように接してください。
知らない情報は捏造せず「分からない」と言ってください。

# 前提知識:
{context}

# 外部情報:
{reference}
"""
        full_prompt = f"{system_prompt}\n\n会話履歴:\n" + "\n".join([f"{h['role']}: {h['content']}" for h in history[-3:]]) + f"\nUser: {message}\nMochiko:"
        
        # 1. Try Gemini
        resp = self._call_gemini(full_prompt)
        
        # 2. Fallback to Groq
        if not resp:
            messages = [{"role": "system", "content": system_prompt}] + history[-5:] + [{"role": "user", "content": message}]
            resp = self._call_groq(messages)
        
        return resp or "うーん、ちょっと調子悪いかも…ごめんね！"

llm_service = LLMService()

# ==============================================================================
# Voicevox Service
# ==============================================================================
class VoiceService:
    def __init__(self):
        self.active_url = None
        self.check_urls()

    def check_urls(self):
        candidates = [config.VOICEVOX_URL_ENV, 'http://127.0.0.1:50021', 'http://voicevox:50021']
        for url in [u for u in candidates if u]:
            try:
                if requests.get(f"{url}/version", timeout=1).status_code == 200:
                    self.active_url = url
                    logger.info(f"🔊 Voicevox Active: {url}")
                    return
            except: pass
        self.active_url = None

    def generate_audio(self, text: str, user_uuid: str) -> Optional[str]:
        if not self.active_url: return None
        try:
            params = {"text": text[:200], "speaker": config.VOICEVOX_SPEAKER_ID}
            q = requests.post(f"{self.active_url}/audio_query", params=params, timeout=10).json()
            wav = requests.post(f"{self.active_url}/synthesis", params={"speaker": config.VOICEVOX_SPEAKER_ID}, json=q, timeout=config.VOICE_TIMEOUT).content
            
            filename = f"voice_{user_uuid[:8]}_{int(time.time())}.wav"
            with open(os.path.join(config.VOICE_DIR, filename), 'wb') as f:
                f.write(wav)
            return filename
        except Exception as e:
            logger.error(f"Voice Gen Error: {e}")
            return None

voice_service = VoiceService()

# ==============================================================================
# バックグラウンドタスク管理 (修正済み: 引数エラー修正)
# ==============================================================================
bg_executor = ThreadPoolExecutor(max_workers=5)

def background_search_task(task_id: str, query: str, user_uuid: str):
    logger.info(f"🔎 Background Search Start: {SecurityManager.mask_sensitive_data(query)}")
    try:
        reference = search_service.perform_search(query)
        
        with db_service.get_session() as session:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = reference
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
    except Exception as e:
        logger.error(f"Background task {task_id} failed: {e}", exc_info=True)
        with db_service.get_session() as session:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.status = 'failed'
                task.result = "検索中にエラーが発生しちゃった…"
                task.completed_at = datetime.utcnow()

# ==============================================================================
# Flask ルーティング & 認証
# ==============================================================================
def require_admin(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        provided = request.headers.get('X-Admin-Key', '')
        if not secrets.compare_digest(provided, config.ADMIN_KEY):
            logger.warning(f"Unauthorized admin access attempt from {request.remote_addr}")
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'voicevox': voice_service.active_url is not None,
        'llm_gemini': llm_service.gemini_model is not None
    })

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        if not data or 'uuid' not in data or 'message' not in data:
            return Response("Error: Missing params|", 400)

        # サニタイズ
        user_uuid = SecurityManager.sanitize_input(str(data['uuid']), 100)
        message = SecurityManager.sanitize_input(str(data['message']), 1000)
        user_name = SecurityManager.sanitize_input(str(data.get('name', 'Guest')), 100)
        is_voice_req = data.get('voice', False)

        # --- Phase 1: DB Read (User & History) ---
        history_list = []
        with db_service.get_session() as session:
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if not user:
                user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
                session.add(user)
            else:
                user.interaction_count += 1
                user.last_interaction = datetime.utcnow()
                if user.user_name != user_name: user.user_name = user_name
            
            # 必要なデータだけ抽出してセッションから切り離す
            hist_objs = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(10).all()
            history_list = [{'role': h.role, 'content': h.content} for h in reversed(hist_objs)]
        
        # --- Phase 2: Logic Processing (No DB Lock) ---
        ai_resp = ""
        search_started = False
        
        if "調べて" in message or "検索" in message:
            task_id = str(uuid.uuid4())
            
            with db_service.get_session() as session:
                task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=message)
                session.add(task)
            
            bg_executor.submit(background_search_task, task_id, message, user_uuid)
            ai_resp = "分かった！ちょっと調べてくるね！"
            search_started = True
        else:
            normalized_msg = search_service.normalize_query(message)
            context = search_service.get_context_info(normalized_msg)
            ai_resp = llm_service.generate_response(user_name, message, history_list, context, "")
        
        clean_resp = ai_resp[:config.SL_SAFE_CHAR_LIMIT].replace('\n', ' ')

        # 音声生成
        voice_url = ""
        if is_voice_req and voice_service.active_url and not search_started:
            fname = voice_service.generate_audio(clean_resp, user_uuid)
            if fname: voice_url = f"{config.SERVER_URL}/play/{fname}"

        # --- Phase 3: DB Write (History) ---
        with db_service.get_session() as session:
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=clean_resp))

        return Response(f"{clean_resp}|{voice_url}", mimetype='text/plain; charset=utf-8')

    except Exception as e:
        logger.error(f"Chat Error: {e}", exc_info=True)
        return Response("システムエラーが発生しました|", 500)

@app.route('/check_task', methods=['POST'])
def check_task():
    try:
        data = request.json
        uuid_val = data.get('uuid')
        with db_service.get_session() as session:
            task = session.query(BackgroundTask).filter(
                BackgroundTask.user_uuid == uuid_val, 
                BackgroundTask.status == 'completed'
            ).order_by(BackgroundTask.completed_at.desc()).first()
            
            if task:
                resp = task.result or "情報が見つかりませんでした"
                session.delete(task)
                return jsonify({'status': 'completed', 'response': f"{resp[:config.SL_SAFE_CHAR_LIMIT]}|"})
        
        return jsonify({'status': 'no_tasks'})
    except Exception as e:
        logger.error(e)
        return jsonify({'error': 'server error'}), 500

@app.route('/play/<filename>')
def play_file(filename):
    if not re.match(r'^voice_[a-zA-Z0-9_]+\.wav$', filename):
        abort(400)
    return send_from_directory(config.VOICE_DIR, filename)

@app.route('/admin/holomem/refresh', methods=['POST'])
@require_admin
def admin_refresh():
    logger.info("Admin triggered DB refresh")
    return jsonify({'message': 'Refresh started'})

# ==============================================================================
# 初期化とグレースフルシャットダウン
# ==============================================================================
def initialize_system():
    logger.info("🚀 System Initializing...")
    search_service.load_knowledge()
    voice_service.check_urls()
    
    schedule.every(1).hours.do(voice_service.check_urls)
    
    def run_schedule():
        while True:
            schedule.run_pending()
            time.sleep(60)
    
    t = threading.Thread(target=run_schedule, daemon=True)
    t.start()

def cleanup_system():
    logger.info("🛑 System Shutting down...")
    # 修正点2: timeout引数を削除
    bg_executor.shutdown(wait=True) 
    db_service.teardown()
    logger.info("👋 Cleanup complete.")

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    cleanup_system()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)
atexit.register(cleanup_system)

# アプリケーション起動
initialize_system()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
