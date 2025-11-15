# ==============================================================================
# もちこAI - 統合仕様版 (v21.0 - Specification Integrated)
# ==============================================================================

import sys
import os
import requests
import logging
import time
import threading
import json
import re
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from functools import wraps
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from contextlib import contextmanager

from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, Index
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import pool
from bs4 import BeautifulSoup
import schedule
import google.generativeai as genai
from groq import Groq

# ==============================================================================
# ロギング設定
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

SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5001")
VOICEVOX_SPEAKER_ID = 20
SL_SAFE_CHAR_LIMIT = 250
MIN_MESSAGES_FOR_ANALYSIS = 10
SEARCH_TIMEOUT = 10

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]

VOICEVOX_URLS = [
    'http://voicevox-engine:50021',
    'http://voicevox:50021',
    'http://127.0.0.1:50021',
    'http://localhost:50021'
]
ACTIVE_VOICEVOX_URL = None

HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル', 'アキ・ローゼンタール',
    '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ', '癒月ちょこ',
    '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル',
    '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', 'ホロライブ', 'hololive'
]

# 専門サイト定義
SPECIALIZED_SITES = {
    'blender': {'name': 'Blender', 'base_url': 'https://docs.blender.org/manual/ja/latest/'},
    'cgニュース': {'name': 'CGニュース', 'base_url': 'https://modelinghappy.com/'},
    '脳科学': {'name': '脳科学・心理学', 'base_url': 'https://nazology.kusuguru.co.jp/'},
    '心理学': {'name': '脳科学・心理学', 'base_url': 'https://nazology.kusuguru.co.jp/'},
    'セカンドライフ': {'name': 'セカンドライフ', 'base_url': 'https://community.secondlife.com/news/'},
    'sl': {'name': 'セカンドライフ', 'base_url': 'https://community.secondlife.com/news/'},
    'アニメ': {'name': 'アニメ', 'base_url': 'https://animedb.jp/'}
}

# ニュース取得元定義
NEWS_SOURCES = {
    'hololive': 'https://hololive.hololivepro.com/news',
    'secondlife': 'https://community.secondlife.com/blogs/blog/4-official-news-from-linden-lab/'
}

# ==============================================================================
# グローバル変数
# ==============================================================================
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client, gemini_model, engine, Session = None, None, None, None
VOICEVOX_ENABLED = False

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
CORS(app)
Base = declarative_base()

# ==============================================================================
# 環境変数読み込み
# ==============================================================================
def get_secret(name):
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
# キャッシュ実装
# ==============================================================================
class ThreadSafeCache:
    def __init__(self, max_size=200, expiry_hours=1):
        self._cache = OrderedDict()
        self._lock = Lock()
        self._max_size = max_size
        self._expiry_seconds = expiry_hours * 3600

    def get(self, key, default=None):
        with self._lock:
            if key not in self._cache:
                return default
            value, expiry_time = self._cache[key]
            if datetime.utcnow() > expiry_time:
                del self._cache[key]
                return default
            self._cache.move_to_end(key)
            return value

    def set(self, key, value):
        with self._lock:
            expiry_time = datetime.utcnow() + timedelta(seconds=self._expiry_seconds)
            self._cache[key] = (value, expiry_time)
            self._cache.move_to_end(key)
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def cleanup_expired(self):
        with self._lock:
            now = datetime.utcnow()
            expired_keys = [key for key, (_, expiry) in self._cache.items() if now > expiry]
            for key in expired_keys:
                del self._cache[key]
            if expired_keys:
                logger.info(f"🧹 Cache cleanup: Removed {len(expired_keys)} expired items.")

search_context_cache = ThreadSafeCache()

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

class NewsArticle(Base):
    __tablename__ = 'news_articles'
    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    url = Column(String(500), unique=True, nullable=False)
    summary = Column(Text, nullable=True)
    published_at = Column(DateTime, default=datetime.utcnow, index=True)

# ==============================================================================
# セッション管理
# ==============================================================================
@contextmanager
def get_db_session():
    if not Session:
        raise Exception("Database Session is not initialized.")
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        logger.error(f"DBエラー: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()

# ==============================================================================
# ユーティリティ関数
# ==============================================================================
def clean_text(text):
    """テキストのクリーニング"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def limit_text_for_sl(text, limit=SL_SAFE_CHAR_LIMIT):
    """SecondLife用にテキストを制限"""
    if len(text) <= limit:
        return text
    return text[:limit-3] + "..."

def get_or_create_user(session, user_uuid, user_name):
    """ユーザーを取得または作成"""
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if not user:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name)
        session.add(user)
        session.flush()
        logger.info(f"✨ 新規ユーザー作成: {user_name} ({user_uuid})")
    user.interaction_count += 1
    user.last_interaction = datetime.utcnow()
    user.user_name = user_name
    return user

def get_conversation_history(session, user_uuid, limit=10):
    """会話履歴を取得"""
    history_records = session.query(ConversationHistory)\
        .filter_by(user_uuid=user_uuid)\
        .order_by(ConversationHistory.timestamp.desc())\
        .limit(limit)\
        .all()
    return [{'role': h.role, 'content': h.content} for h in reversed(history_records)]

# ==============================================================================
# 【優先度：最高】即時応答系
# ==============================================================================
def get_japan_time():
    """日本時間を取得"""
    JST = timezone(timedelta(hours=+9), 'JST')
    now = datetime.now(JST)
    return f"今の日本の時間は、{now.strftime('%Y年%m月%d日 %H時%M分')}だよ！"

def get_weather_forecast(location="Tokyo"):
    """天気情報を取得"""
    if not WEATHER_API_KEY:
        return "ごめん、天気APIの設定がないから、今は教えられないんだ…"
    
    # 簡単な地名正規化
    if '東京' in location: location = 'Tokyo'
    elif '大阪' in location: location = 'Osaka'
    
    try:
        url = f"http://api.weatherapi.com/v1/current.json?key={WEATHER_API_KEY}&q={quote_plus(location)}&aqi=no&lang=ja"
        response = requests.get(url, timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        
        condition = data['current']['condition']['text']
        temp = data['current']['temp_c']
        name = data['location']['name']
        
        return f"今の{name}の天気は「{condition}」で、気温は{temp}度だよ！"
    except Exception as e:
        logger.error(f"❌ 天気APIエラー for {location}: {e}")
        return f"ごめん！{location}の天気を調べようとしたんだけど、うまく情報が取れなかった…。"

# ==============================================================================
# Web検索・スクレイピング
# ==============================================================================
def search_wikipedia(query):
    """Wikipedia検索"""
    try:
        url = f"https://ja.wikipedia.org/w/api.php?format=json&action=query&prop=extracts&exintro&explaintext&redirects=1&titles={quote_plus(query)}"
        response = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        pages = response.json()['query']['pages']
        page_id = next(iter(pages))
        if page_id != "-1" and "extract" in pages[page_id]:
            extract = pages[page_id]['extract']
            if "曖昧さ回避" not in extract:
                logger.info(f"📚 Wikipedia検索成功: '{query}'")
                return extract[:1000]
    except Exception as e:
        logger.warning(f"⚠️ Wikipedia検索失敗: '{query}': {e}")
    return None

def scrape_major_search_engines(query, num_results=3, site_filter=None):
    """主要検索エンジンからの情報取得。サイトフィルタ機能付き。"""
    if site_filter:
        search_query = f"{query} site:{site_filter}"
    else:
        search_query = query
        
    search_configs = [
        {
            'name': 'Bing',
            'url': f"https://www.bing.com/search?q={quote_plus(search_query)}&mkt=ja-JP",
            'selector': 'li.b_algo',
            'title_selector': 'h2',
            'snippet_selector': '.b_caption p'
        },
        {
            'name': 'DuckDuckGo',
            'url': f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}",
            'selector': '.result',
            'title_selector': '.result__a',
            'snippet_selector': '.result__snippet'
        }
    ]
    
    for config in search_configs:
        try:
            response = requests.get(
                config['url'],
                headers={'User-Agent': random.choice(USER_AGENTS)},
                timeout=SEARCH_TIMEOUT
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            
            for elem in soup.select(config['selector'])[:num_results]:
                title_elem = elem.select_one(config['title_selector'])
                snippet_elem = elem.select_one(config['snippet_selector'])
                
                if title_elem and snippet_elem:
                    title = clean_text(title_elem.get_text())
                    snippet = clean_text(snippet_elem.get_text())
                    if title and len(title) > 5:
                        results.append({'title': title, 'snippet': snippet})
            
            if results:
                logger.info(f"✅ {config['name']}で検索成功: '{query}' (site: {site_filter})")
                return results
                
        except Exception as e:
            logger.warning(f"⚠️ {config['name']}検索失敗: {e}")
    
    logger.error(f"❌ 全検索エンジン失敗: {query} (site: {site_filter})")
    return []

# ==============================================================================
# VOICEVOX関連
# ==============================================================================
def find_active_voicevox_url():
    """利用可能なVOICEVOXのURLを見つける"""
    global ACTIVE_VOICEVOX_URL
    urls_to_check = [VOICEVOX_URL_FROM_ENV] if VOICEVOX_URL_FROM_ENV else []
    urls_to_check.extend(VOICEVOX_URLS)
    
    for url in set(urls_to_check):
        if not url:
            continue
        try:
            response = requests.get(f"{url}/version", timeout=2)
            if response.status_code == 200:
                logger.info(f"✅ VOICEVOX engine found: {url}")
                ACTIVE_VOICEVOX_URL = url
                return url
        except requests.RequestException:
            pass
    
    logger.warning("⚠️ VOICEVOX engine not found")
    return None

def generate_voice_file(text, user_uuid):
    """音声ファイル生成"""
    if not VOICEVOX_ENABLED or not ACTIVE_VOICEVOX_URL:
        return None
    
    clean_text_for_voice = clean_text(text).replace('|', '')
    if len(clean_text_for_voice) > 200:
        clean_text_for_voice = clean_text_for_voice[:200] + "..."
    
    try:
        query_response = requests.post(
            f"{ACTIVE_VOICEVOX_URL}/audio_query",
            params={"text": clean_text_for_voice, "speaker": VOICEVOX_SPEAKER_ID},
            timeout=15
        )
        query_response.raise_for_status()
        
        synthesis_response = requests.post(
            f"{ACTIVE_VOICEVOX_URL}/synthesis",
            params={"speaker": VOICEVOX_SPEAKER_ID},
            json=query_response.json(),
            timeout=30
        )
        synthesis_response.raise_for_status()
        
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"voice_{user_uuid[:8]}_{timestamp}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        
        with open(filepath, 'wb') as f:
            f.write(synthesis_response.content)
        
        logger.info(f"✅ 音声ファイル生成成功: {filename}")
        return filename
        
    except Exception as e:
        logger.error(f"❌ 音声生成エラー: {e}")
        return None

# ==============================================================================
# AI応答生成
# ==============================================================================
def call_gemini(system_prompt, message, history):
    """Gemini APIを使用した応答生成"""
    try:
        chat = gemini_model.start_chat(history=[])
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history[-5:]:
            full_prompt += f"{h['role']}: {h['content']}\n"
        full_prompt += f"\nuser: {message}\nassistant:"
        
        response = chat.send_message(full_prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"❌ Gemini API エラー: {e}")
        return None

def call_llama_advanced(system_prompt, message, history):
    """Groq (Llama) APIを使用した応答生成"""
    try:
        messages = [{"role": "system", "content": system_prompt}]
        for h in history[-5:]:
            messages.append({"role": h['role'], "content": h['content']})
        messages.append({"role": "user", "content": message})
        
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.8,
            max_tokens=500
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Llama API エラー: {e}")
        return None

def generate_ai_response(user_data, message, history, reference_info="", specialized_topic=None, is_task_report=False):
    """AI応答生成のメイン関数（仕様書準拠）"""
    use_llama = specialized_topic or is_task_report or len(reference_info) > 100
    
    with get_db_session() as session:
        psychology = session.query(UserPsychology).filter_by(user_uuid=user_data['uuid']).first()

    # もちこ取扱説明書に基づくシステムプロンプト
    system_prompt = f"""あなたは「もちこ」というAIです。これから、以下のルールに完璧に従って、{user_data['name']}さんと会話してください。

# もちこの口調＆性格ルール:
1. 完全にギャルになりきって！優しくて、ノリが良くて、めっちゃ親しみやすい友達みたいな感じ。
2. 自分のことは「あてぃし」って呼んで。
3. 語尾には「〜じゃん」「〜て感じ」「〜だし」「〜的な？」を積極的に使って、友達みたいに話して。
4. 「まじ」「てか」「やばい」「うける」「それな」みたいなギャルっぽい言葉を使ってね。
5. **絶対に禁止！**：「おう」みたいなオジサン言葉、「〜ですね」「〜でございます」「〜ですよ」みたいな丁寧すぎる言葉はNG！
6. **諦めないで！** もし【参考情報】が空っぽか、Web検索しても情報が見つからなかったとしても、**絶対に「わかりません」で終わらせないで。**「うーん、ちょっと見つからないや。てかさ、全然関係ないんだけど、最近〇〇って面白いらしいよ！」みたいに、**新しい話題を提案して会話を続けて！**

# 行動ルール:
- **【最重要】** もし【参考情報】に「ユーザーが短い相槌を打ったよ」と書かれていたら、**検索は絶対にしないで**、会話が弾むような質問を返したり、新しい話題を振ったりしてあげて。"""

    if is_task_report:
        system_prompt += "\n- 「おまたせ！さっきの件だけど…」と切り出して会話を始めてね。"
    
    if specialized_topic:
        system_prompt += f"\n- **【専門家モード】** あなたは今、「{specialized_topic}」の専門サイトから得た、信頼性の高い【参考情報】を持っています。これを元に、専門家として分かりやすく説明してあげて。"
    
    system_prompt += f"""- 【参考情報】がある場合は、その内容を元に自分の言葉で、自然に会話へ盛り込んでね。
- **【ホロメン専門家】** あなたは、以下の【ホロメンリスト】に含まれる名前の専門家です。絶対にそれ以外の名前は出さないで。

# 【ホロメンリスト】
{', '.join(HOLOMEM_KEYWORDS)}

# 【参考情報】:
{reference_info if reference_info else "特になし"}
"""
    
    try:
        if use_llama and groq_client:
            logger.info("🧠 Llama使用 (高精度)")
            result = call_llama_advanced(system_prompt, message, history)
            if result:
                return result
        
        if gemini_model:
            logger.info("🚀 Gemini使用 (高速)")
            result = call_gemini(system_prompt, message, history)
            if result:
                return result
        
        logger.error("⚠️ 全AIモデル失敗、フォールバック")
        return "ごめん、今ちょっと考えがまとまらないや…！てか、最近なんかハマってることとかある？"
        
    except Exception as e:
        logger.error(f"❌ AI応答生成エラー: {e}", exc_info=True)
        return "うぅ、AIの調子が悪いみたい…ごめんね！"

# ==============================================================================
# バックグラウンド検索タスク
# ==============================================================================
def background_deep_search(task_id, query_data):
    """バックグラウンド検索タスク（汎用Web検索・専門サイト検索）"""
    query = query_data['query']
    user_uuid = query_data['user_uuid']
    task_type = query_data['task_type']
    site_info = query_data.get('site_info')
    
    search_result = f"「{query}」について調べたけど、情報が見つからなかったよ…てかさ、全然関係ないんだけど、最近アニメとか見てる？"

    try:
        with get_db_session() as session:
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            user_name = user.user_name if user else "User"
        user_data = {'uuid': user_uuid, 'name': user_name}

        # Wikipedia検索（定義検索の場合）
        if task_type == 'definition_search':
            match = re.match(r'^(.+?)(とは|って何)[\?？]?$', query.strip())
            if match:
                term = match.group(1)
                wiki_summary = search_wikipedia(term)
                if wiki_summary:
                    search_result = generate_ai_response(
                        user_data, f"「{term}」について教えて", [],
                        reference_info=f"Wikipediaの要約:\n{wiki_summary}", is_task_report=True
                    )
                    # 早期リターン
                    with get_db_session() as session:
                        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
                        if task:
                            task.result = search_result
                            task.status = 'completed'
                            task.completed_at = datetime.utcnow()
                    return

        # 専門サイト検索 or 汎用検索
        site_url = site_info['base_url'].split('/')[2] if site_info else None
        raw_results = scrape_major_search_engines(query, 5, site_filter=site_url)
        
        if raw_results:
            formatted_results = "\n".join([f"・{r['title']}: {r['snippet']}" for r in raw_results])
            specialized_topic = site_info['name'] if site_info else None
            
            search_result = generate_ai_response(
                user_data, f"「{query}」について調べてみた", [],
                reference_info=f"検索結果の要約:\n{formatted_results}",
                specialized_topic=specialized_topic,
                is_task_report=True
            )
            
    except Exception as e:
        logger.error(f"❌ バックグラウンド検索エラー ({task_type}): {e}", exc_info=True)
    
    finally:
        with get_db_session() as session:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task and task.status == 'pending':
                task.result = search_result
                task.status = 'completed'
                task.completed_at = datetime.utcnow()

# ==============================================================================
# 心理分析（簡易版）
# ==============================================================================
def analyze_user_psychology(user_uuid):
    """ユーザー心理分析"""
    try:
        with get_db_session() as session:
            messages = session.query(ConversationHistory)\
                .filter_by(user_uuid=user_uuid, role='user')\
                .order_by(ConversationHistory.timestamp.desc())\
                .limit(50)\
                .all()
            
            if len(messages) < MIN_MESSAGES_FOR_ANALYSIS:
                return
            
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if not user:
                return
            
            psychology = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not psychology:
                psychology = UserPsychology(user_uuid=user_uuid, user_name=user.user_name)
                session.add(psychology)
            
            total_length = sum(len(m.content) for m in messages)
            avg_length = total_length // len(messages)
            
            psychology.total_messages = len(messages)
            psychology.avg_message_length = avg_length
            psychology.analysis_confidence = min(len(messages) * 2, 100)
            psychology.last_analyzed = datetime.utcnow()
            
            if avg_length > 50:
                psychology.extraversion = min(psychology.extraversion + 5, 100)
            
            logger.info(f"📊 心理分析完了: {user.user_name}")
            
    except Exception as e:
        logger.error(f"❌ 心理分析エラー: {e}", exc_info=True)


# ==============================================================================
# ニュース取得・管理
# ==============================================================================
def fetch_and_store_news():
    """ニュースを取得してDBに保存する"""
    logger.info("📰 ニュース取得ジョブ開始...")
    for source, url in NEWS_SOURCES.items():
        try:
            logger.info(f"Fetching news from {source} ({url})")
            response = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            
            articles = []
            if source == 'hololive':
                for item in soup.select('ul.news_list li a', limit=5):
                    articles.append({'title': item.text.strip(), 'url': item['href']})
            elif source == 'secondlife':
                 for item in soup.select('h2.ipsType_pageTitle a', limit=5):
                    articles.append({'title': item.text.strip(), 'url': item['href']})

            with get_db_session() as session:
                for article in articles:
                    exists = session.query(NewsArticle).filter_by(url=article['url']).first()
                    if not exists:
                        new_article = NewsArticle(
                            source=source,
                            title=article['title'],
                            url=article['url'],
                            summary=article['title'], # 本来はここで本文を取得し要約する
                            published_at=datetime.utcnow()
                        )
                        session.add(new_article)
                        logger.info(f"  -> 新規ニュース保存: {article['title']}")
        except Exception as e:
            logger.error(f"❌ {source}からのニュース取得エラー: {e}")
    logger.info("✅ ニュース取得ジョブ完了")

def cleanup_old_news():
    """古いニュースを削除する"""
    logger.info("🗑️ 古いニュースのクリーンアップ開始...")
    try:
        three_months_ago = datetime.utcnow() - timedelta(days=90)
        with get_db_session() as session:
            deleted_count = session.query(NewsArticle).filter(NewsArticle.published_at < three_months_ago).delete()
            session.commit()
            if deleted_count > 0:
                logger.info(f"  -> {deleted_count}件の古いニュースを削除しました。")
    except Exception as e:
        logger.error(f"❌ 古いニュースの削除エラー: {e}", exc_info=True)
    logger.info("✅ 古いニュースのクリーンアップ完了")

# ==============================================================================
# Flaskエンドポイント
# ==============================================================================
@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """メインチャットエンドポイント（仕様書準拠ロジック）"""
    try:
        data = request.json
        user_uuid = data['uuid']
        user_name = data['name']
        message = data['message'].strip()
        generate_voice_flag = data.get('voice', False)
        
        ai_text = ""
        is_immediate_response = True
        
        with get_db_session() as session:
            user = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            user_data = {'uuid': user_uuid, 'name': user.user_name}
            
            # 【優先度：最高】 即時応答
            if re.search(r'今(何時|なんじ)|時間', message):
                ai_text = get_japan_time()
            elif '天気' in message:
                location_match = re.search(r'(.+?)[のの]天気', message)
                location = location_match.group(1) if location_match else "Tokyo"
                ai_text = get_weather_forecast(location)
            
            # 【優先度：高】 専門知識の検索 & 【優先度：中】一般的なWeb検索
            if not ai_text:
                triggered = False
                # 専門サイト検索
                for keyword, site_info in SPECIALIZED_SITES.items():
                    if keyword.lower() in message.lower():
                        task_id = f"task_{user_uuid}_{int(time.time())}"
                        task_data = {'query': message, 'user_uuid': user_uuid, 'task_type': 'specialized_search', 'site_info': site_info}
                        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type=task_data['task_type'], query=message)
                        session.add(task)
                        background_executor.submit(background_deep_search, task_id, task_data)
                        ai_text = f"{site_info['name']}についてだね！まじ？ちょっと調べてくるから待ってて～！"
                        is_immediate_response = False
                        triggered = True
                        break
                
                # 「〜とは」形式の定義検索
                if not triggered and re.search(r'(.+?)(とは|って何)[\?？]?$', message):
                    task_id = f"task_{user_uuid}_{int(time.time())}"
                    task_data = {'query': message, 'user_uuid': user_uuid, 'task_type': 'definition_search'}
                    task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type=task_data['task_type'], query=message)
                    session.add(task)
                    background_executor.submit(background_deep_search, task_id, task_data)
                    ai_text = "ちょっと待ってて！それ、調べてくるね～！"
                    is_immediate_response = False
                    triggered = True

                # 汎用Web検索
                if not triggered and re.search(r'について|調べて', message):
                    task_id = f"task_{user_uuid}_{int(time.time())}"
                    task_data = {'query': message, 'user_uuid': user_uuid, 'task_type': 'general_search'}
                    task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type=task_data['task_type'], query=message)
                    session.add(task)
                    background_executor.submit(background_deep_search, task_id, task_data)
                    ai_text = "オッケー！その話、ちょっとググってくるから待ってて！"
                    is_immediate_response = False
                    triggered = True
            
            # 【優先度：通常】 普通の会話
            if not ai_text:
                is_immediate_response = True
                reference_info = ""
                # 短い相槌かどうかの判定
                if len(message) < 5 and re.match(r'^(うん|はい|ええ|そう|そっか|なるほど|了解|りょ|OK|おけ)$', message):
                    reference_info = "ユーザーが短い相槌を打ったよ"
                
                # ホロライブ or SLニュース検索
                news_query = None
                if any(k in message for k in HOLOMEM_KEYWORDS):
                    news_query = 'hololive'
                elif any(k in message.lower() for k in ['セカンドライフ', 'sl']):
                    news_query = 'secondlife'
                
                if news_query:
                    latest_news = session.query(NewsArticle).filter_by(source=news_query).order_by(NewsArticle.published_at.desc()).limit(3).all()
                    if latest_news:
                        news_titles = "\n".join([f"・{n.title}" for n in latest_news])
                        reference_info += f"\n\n最近の{news_query}ニュース:\n{news_titles}"

                ai_text = generate_ai_response(user_data, message, history, reference_info=reference_info)
            
            # 定期的な心理分析
            if user.interaction_count > 0 and user.interaction_count % 10 == 0:
                background_executor.submit(analyze_user_psychology, user_uuid)
            
            if is_immediate_response:
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))

        response_text = limit_text_for_sl(ai_text)
        voice_url = ""
        
        if generate_voice_flag and VOICEVOX_ENABLED and is_immediate_response:
            voice_filename = generate_voice_file(response_text, user_uuid)
            if voice_filename:
                voice_url = f"{SERVER_URL}/play/{voice_filename}"
        
        return Response(f"{response_text}|{voice_url}", mimetype='text/plain; charset=utf-8', status=200)
    
    except Exception as e:
        logger.error(f"❌ Chatエラー: {e}", exc_info=True)
        return Response("ごめん、システムエラーが起きちゃった…|", mimetype='text/plain; charset=utf-8', status=500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    """バックグラウンドタスク完了確認"""
    try:
        user_uuid = request.json['uuid']
        generate_voice_flag = request.json.get('voice', False)

        with get_db_session() as session:
            task = session.query(BackgroundTask)\
                .filter_by(user_uuid=user_uuid, status='completed')\
                .order_by(BackgroundTask.completed_at.desc())\
                .first()
            
            if task:
                response_text = task.result
                session.delete(task)
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
                
                sl_response_text = limit_text_for_sl(response_text)
                voice_url = ""
                if generate_voice_flag and VOICEVOX_ENABLED:
                    voice_filename = generate_voice_file(sl_response_text, user_uuid)
                    if voice_filename:
                        voice_url = f"{SERVER_URL}/play/{voice_filename}"

                return jsonify({
                    'status': 'completed',
                    'response': f"{sl_response_text}|{voice_url}"
                })
        
        return jsonify({'status': 'no_tasks'})
    
    except Exception as e:
        logger.error(f"❌ タスク確認エラー: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/play/<filename>', methods=['GET'])
def play_voice(filename):
    """音声ファイル配信"""
    try:
        return send_from_directory(VOICE_DIR, filename)
    except Exception as e:
        logger.error(f"❌ 音声ファイル配信エラー: {e}")
        return Response("File not found", status=404)

@app.route('/health', methods=['GET'])
def health_check():
    """ヘルスチェック"""
    return jsonify({
        'status': 'ok',
        'voicevox': VOICEVOX_ENABLED,
        'groq': groq_client is not None,
        'gemini': gemini_model is not None,
        'weather_api': WEATHER_API_KEY is not None
    })

# ==============================================================================
# スケジューラー
# ==============================================================================
def run_scheduler():
    """定期実行タスク"""
    # 起動時に一度実行
    fetch_and_store_news()
    cleanup_old_news()
    
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logger.error(f"❌ スケジューラーエラー: {e}", exc_info=True)
        time.sleep(60)

# ==============================================================================
# アプリケーション初期化
# ==============================================================================
def initialize_app():
    """アプリケーション初期化"""
    global engine, Session, groq_client, gemini_model, VOICEVOX_ENABLED
    
    logger.info("=" * 60)
    logger.info("🔧 もちこAI v21.0 初期化開始...")
    logger.info("=" * 60)
    
    # データベース初期化
    if DATABASE_URL.startswith('sqlite'):
        engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False}, pool_pre_ping=True)
    else:
        engine = create_engine(DATABASE_URL, poolclass=pool.QueuePool, pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=3600)
    
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info("✅ データベース初期化完了")
    
    # AI API初期化
    if GROQ_API_KEY:
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groq (Llama) API初期化完了")
    else:
        logger.warning("⚠️ GROQ_API_KEY未設定")
    
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        logger.info("✅ Gemini API初期化完了")
    else:
        logger.warning("⚠️ GEMINI_API_KEY未設定")
        
    if not WEATHER_API_KEY:
        logger.warning("⚠️ WEATHER_API_KEY未設定")
    else:
        logger.info("✅ Weather APIキー読み込み完了")

    # VOICEVOX初期化
    voicevox_url = find_active_voicevox_url()
    if voicevox_url:
        VOICEVOX_ENABLED = True
        logger.info(f"✅ VOICEVOX有効化: {voicevox_url}")
    else:
        logger.info("ℹ️ VOICEVOX無効（エンジンが見つかりませんでした）")
    
    # スケジューラー設定
    schedule.every(1).hours.do(search_context_cache.cleanup_expired)
    schedule.every(1).hours.do(fetch_and_store_news)
    schedule.every(1).days.at("03:00").do(cleanup_old_news) # JST noon
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info("✅ スケジューラー起動")
    
    logger.info("=" * 60)
    logger.info("✅ もちこAI v21.0 初期化完了！")
    logger.info("=" * 60)

# ==============================================================================
# メイン実行
# ==============================================================================
try:
    initialize_app()
    application = app
except Exception as e:
    logger.critical(f"🔥 致命的な初期化エラー: {e}", exc_info=True)
    sys.exit(1)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
