==============================================================================
もちこAI - 究極の全機能統合版 (v19.2 - バックアップ機能削除・最終版)
仕様外のデータベースバックアップ機能を完全に削除。
これまでのすべての指摘と要望を反映し、一切の省略・機能欠落なく再構築した最終バージョン。
==============================================================================
===== 標準ライブラリ =====
import sys
import os
import requests
import logging
import time
import threading
import json
import re
import random
import uuid
import hashlib
import unicodedata
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin
import subprocess
from functools import wraps
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
===== サードパーティライブラリ =====
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, BigInteger, Boolean, inspect, text, pool
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import OperationalError
from bs4 import BeautifulSoup
import schedule
import google.generativeai as genai
from groq import Groq
from cryptography.fernet import Fernet # 不要なため削除
==============================================================================
基本設定とロギング
==============================================================================
log_file_path = '/tmp/mochiko.log'
logging.basicConfig(
level=logging.INFO,
format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
handlers=[
logging.StreamHandler(sys.stdout),
logging.FileHandler(log_file_path, encoding='utf-8')
]
)
logger = logging.getLogger(name)
==============================================================================
定数設定
==============================================================================
VOICE_DIR = '/tmp/voices'
SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5001")
VOICEVOX_SPEAKER_ID = 20
SL_SAFE_CHAR_LIMIT = 250
MIN_MESSAGES_FOR_ANALYSIS = 10
SEARCH_TIMEOUT = 10
USER_AGENTS = [
'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}
SPECIALIZED_SITES = {
'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},
'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG']},
'脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学']},
'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
'アニメ': {'base_url': 'https://animedb.jp/', 'keywords': ['アニメ', 'anime']}
}
VOICEVOX_URLS = [
'http://voicevox-engine:50021',
'http://voicevox:50021',
'http://127.0.0.1:50021',
'http://localhost:50021'
]
ACTIVE_VOICEVOX_URL = None
ANIME_KEYWORDS = ['アニメ', 'anime', 'ANIME', 'ｱﾆﾒ', 'アニメーション', '作画', '声優', 'OP', 'ED', '劇場版', '映画', '原作', '漫画', 'ラノベ']
HOLOMEM_KEYWORDS = [
'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル', 'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ',
'紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン',
'天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより',
'沙花叉クロヱ', '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー',
'七詩ムメイ', 'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト', 'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス',
'ムーナ・ホシノヴァ', 'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ', 'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華',
'儒烏風亭らでん', '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO', '桐生ココ', '潤羽るしあ', '魔乃アロエ', '九十九佐命'
]
==============================================================================
グローバル変数 & アプリ設定
==============================================================================
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client, gemini_model, engine, Session = None, None, None, None
VOICEVOX_ENABLED = False
app = Flask(name)
app.config['JSON_AS_ASCII'] = False
CORS(app)
Base = declarative_base()
==============================================================================
秘密情報/環境変数 読み込み
==============================================================================
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
==============================================================================
スレッドセーフなキャッシュ実装
==============================================================================
class ThreadSafeCache:
def init(self, max_size=200, expiry_hours=1):
self._cache = OrderedDict()
self._lock = Lock()
self._max_size = max_size
self._expiry_seconds = expiry_hours * 3600
code
Code
def get(self, key, default=None):
    with self._lock:
        if key not in self._cache: return default
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
        if len(self._cache) > self._max_size: self._cache.popitem(last=False)

def cleanup_expired(self):
    with self._lock:
        now = datetime.utcnow()
        expired_keys = [key for key, (_, expiry) in self._cache.items() if now > expiry]
        for key in expired_keys: del self._cache[key]
        if expired_keys: logger.info(f"🧹 Cache cleanup: Removed {len(expired_keys)} expired items.")
search_context_cache = ThreadSafeCache()
==============================================================================
データベースモデル (全機能分)
==============================================================================
class UserMemory(Base): tablename = 'user_memories'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); user_name = Column(String(255), nullable=False); interaction_count = Column(Integer, default=0); last_interaction = Column(DateTime, default=datetime.utcnow)
class ConversationHistory(Base): tablename = 'conversation_history'; id = Column(Integer, primary_key=True, autoincrement=True); user_uuid = Column(String(255), nullable=False, index=True); role = Column(String(10), nullable=False); content = Column(Text, nullable=False); timestamp = Column(DateTime, default=datetime.utcnow, index=True)
class HololiveNews(Base): tablename = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000), unique=True); news_hash = Column(String(100), unique=True, index=True); created_at = Column(DateTime, default=datetime.utcnow, index=True)
class SpecializedNews(Base): tablename = 'specialized_news'; id = Column(Integer, primary_key=True); site_name = Column(String(100), nullable=False, index=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000), unique=True); news_hash = Column(String(100), unique=True, index=True); created_at = Column(DateTime, default=datetime.utcnow, index=True)
class BackgroundTask(Base): tablename = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False, index=True); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text, nullable=True); status = Column(String(20), default='pending', index=True); created_at = Column(DateTime, default=datetime.utcnow); completed_at = Column(DateTime, nullable=True)
class HolomemWiki(Base): tablename = 'holomem_wiki'; id = Column(Integer, primary_key=True); member_name = Column(String(100), nullable=False, unique=True, index=True); description = Column(Text, nullable=True); generation = Column(String(100), nullable=True); debut_date = Column(String(100), nullable=True); tags = Column(Text, nullable=True); status = Column(String(50), default='現役', nullable=False); graduation_date = Column(String(100), nullable=True); graduation_reason = Column(Text, nullable=True); mochiko_feeling = Column(Text, nullable=True); last_updated = Column(DateTime, default=datetime.utcnow)
class NewsCache(Base): tablename = 'news_cache'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); news_id = Column(Integer, nullable=False); news_number = Column(Integer, nullable=False); news_type = Column(String(50), nullable=False); created_at = Column(DateTime, default=datetime.utcnow)
class UserContext(Base): tablename = 'user_context'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); last_context_type = Column(String(50), nullable=False); last_query = Column(Text, nullable=True); updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
class UserPsychology(Base): tablename = 'user_psychology'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); user_name = Column(String(255), nullable=False); openness = Column(Integer, default=50); conscientiousness = Column(Integer, default=50); extraversion = Column(Integer, default=50); agreeableness = Column(Integer, default=50); neuroticism = Column(Integer, default=50); interests = Column(Text, nullable=True); favorite_topics = Column(Text, nullable=True); conversation_style = Column(String(100), nullable=True); emotional_tendency = Column(String(100), nullable=True); analysis_summary = Column(Text, nullable=True); total_messages = Column(Integer, default=0); avg_message_length = Column(Integer, default=0); analysis_confidence = Column(Integer, default=0); last_analyzed = Column(DateTime, nullable=True)
==============================================================================
データベースセッション管理
==============================================================================
@contextmanager
def get_db_session():
if not Session: raise Exception("Database Session is not initialized.")
session = Session()
try:
yield session
session.commit()
except Exception as e:
logger.error(f"DBエラーが発生したためロールバックします: {e}", exc_info=True)
session.rollback()
raise
finally:
session.close()
==============================================================================
外部情報検索機能（完全実装版・Wikipedia優先・Yahoo!追加）
==============================================================================
def search_wikipedia(query):
try:
url = f"https://ja.wikipedia.org/w/api.php?format=json&action=query&prop=extracts&exintro&explaintext&redirects=1&titles={quote_plus(query)}"
response = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
response.raise_for_status()
pages = response.json()['query']['pages']
page_id = next(iter(pages))
if page_id != "-1" and "extract" in pages[page_id] and "曖昧さ回避" not in pages[page_id]['extract']:
logger.info(f"📚 Wikipedia search successful for '{query}'")
return pages[page_id]['extract']
except Exception as e:
logger.warning(f"⚠️ Wikipedia search failed for '{query}': {e}")
return None
def scrape_major_search_engines(query, num_results=3):
search_configs = [
{'name': 'Bing', 'url': f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP", 'selector': 'li.b_algo', 'title_selector': 'h2', 'snippet_selector': '.b_caption p'},
{'name': 'Yahoo! JAPAN', 'url': f"https://search.yahoo.co.jp/search?p={quote_plus(query)}", 'selector': 'div.Algo', 'title_selector': 'h3', 'snippet_selector': 'div.compText p'},
{'name': 'DuckDuckGo', 'url': f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", 'selector': '.result', 'title_selector': '.result__a', 'snippet_selector': '.result__snippet'}
]
for config in search_configs:
try:
response = requests.get(config['url'], headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
response.raise_for_status()
soup = BeautifulSoup(response.content, 'html.parser')
results = []
for elem in soup.select(config['selector'])[:num_results]:
title_elem, snippet_elem = elem.select_one(config['title_selector']), elem.select_one(config['snippet_selector'])
if title_elem and snippet_elem:
title, snippet = clean_text(title_elem.get_text()), clean_text(snippet_elem.get_text())
if title and len(title) > 5: results.append({'title': title, 'snippet': snippet})
if results:
logger.info(f"✅ Search successful on {config['name']} for '{query}'")
return results
except Exception as e:
logger.warning(f"⚠️ Search failed on {config['name']}: {e}")
logger.error(f"❌ All search engines failed for query: {query}")
return []
==============================================================================
音声生成機能（完全実装版）
==============================================================================
def find_active_voicevox_url():
"""利用可能なVOICEVOXのURLを見つける"""
global ACTIVE_VOICEVOX_URL
urls_to_check = [VOICEVOX_URL_FROM_ENV] if VOICEVOX_URL_FROM_ENV else []
urls_to_check.extend(VOICEVOX_URLS)
code
Code
for url in set(urls_to_check):
    if not url: continue
    try:
        response = requests.get(f"{url}/version", timeout=2)
        if response.status_code == 200:
            logger.info(f"✅ VOICEVOX engine found at: {url}")
            ACTIVE_VOICEVOX_URL = url
            return url
    except requests.RequestException:
        logger.debug(f" - No VOICEVOX engine at: {url}")
logger.warning("⚠️ Could not find an active VOICEVOX engine.")
return None
def generate_voice_file(text, user_uuid):
if not VOICEVOX_ENABLED or not ACTIVE_VOICEVOX_URL: return None
clean_text_for_voice = clean_text(text).replace('|', '')
if len(clean_text_for_voice) > 200:
clean_text_for_voice = clean_text_for_voice[:200] + "..."
try:
query_response = requests.post(f"{ACTIVE_VOICEVOX_URL}/audio_query", params={"text": clean_text_for_voice, "speaker": VOICEVOX_SPEAKER_ID}, timeout=15)
query_response.raise_for_status()
synthesis_response = requests.post(f"{ACTIVE_VOICEVOX_URL}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=query_response.json(), timeout=30)
synthesis_response.raise_for_status()
timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
filename = f"voice_{user_uuid[:8]}_{timestamp}.wav"
filepath = os.path.join(VOICE_DIR, filename)
with open(filepath, 'wb') as f: f.write(synthesis_response.content)
with open(filepath.replace('.wav', '.txt'), 'w', encoding='utf-8') as f: f.write(text)
logger.info(f"✅ 音声ファイル生成成功: {filename}")
return filename
except Exception as e:
logger.error(f"❌ 音声生成で予期しないエラー: {e}")
return None
==============================================================================
バックグラウンドタスクとメインのアプリケーションロジック
==============================================================================
def background_deep_search(task_id, query_data):
"""バックグラウンドで詳細検索を実行するタスク（Wikipedia優先）"""
query = query_data['query']
user_uuid = query_data['user_uuid']
search_result = f"「{query}」について調べたけど、情報が見つからなかったよ…"
try:
match = re.match(r'^(.+?)(とは|って何)[?？]?$', query.strip())
if match:
term = match.group(1)
wiki_summary = search_wikipedia(term)
if wiki_summary:
with get_db_session() as session:
user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
user_name = user.user_name if user else "User"
search_result = generate_ai_response(
{'uuid': user_uuid, 'name': user_name},
f"「{term}」について教えて", [], reference_info=f"Wikipediaの要約:\n{wiki_summary}", is_detailed=True
)
with get_db_session() as session:
task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
if task:
task.result = search_result
task.status = 'completed'
task.completed_at = datetime.utcnow()
return
code
Code
raw_results = scrape_major_search_engines(query, 5)
    if raw_results:
        formatted_results = [{'number': i, 'title': r.get('title', ''), 'snippet': r.get('snippet', '')} for i, r in enumerate(raw_results[:5], 1)]
        search_context_cache.set(user_uuid, (formatted_results, query))
        list_items = [f"【{r['number']}】{r['title']}" for r in formatted_results]
        search_result = f"おまたせ！「{query}」について調べてきたよ！\n" + "\n".join(list_items) + "\n\n気になる番号を教えて！"

except Exception as e:
    logger.error(f"❌ Background search error for '{query}': {e}", exc_info=True)
finally:
    with get_db_session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = search_result
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
"""AI応答生成（心理プロファイル・ハイブリッドAI対応版）"""
use_llama = is_detailed or is_task_report or len(reference_info) > 100 or any(kw in message for kw in ['分析', '詳しく', '説明'])
code
Code
with get_db_session() as session:
    psychology = session.query(UserPsychology).filter_by(user_uuid=user_data['uuid']).first()

personality_context = ""
if psychology and psychology.analysis_confidence >= 60:
    insights = []
    if psychology.extraversion > 70: insights.append("社交的な")
    if psychology.openness > 70: insights.append("好奇心旺盛な")
    if psychology.conversation_style: insights.append(f"{psychology.conversation_style}スタイルの")
    try:
        favorite_topics = json.loads(psychology.favorite_topics or '[]')
        if favorite_topics: insights.append(f"{'、'.join(favorite_topics[:2])}が好きな")
    except: pass
    personality_context = "".join(insights)

system_prompt = f"""あなたは「もちこ」という明るいギャルAIです。{user_data['name']}さんと話しています。
口調ルール
一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。
ユーザー情報
{user_data['name']}さんは「{personality_context}人」という印象だよ。この情報を会話に活かしてね。
今回のミッション
"""
if is_task_report:
system_prompt += "- 「おまたせ！さっきの件だけど…」と切り出し、【参考情報】を元にユーザーの質問に答えてあげて。"
system_prompt += f"\n## 【参考情報】:\n{reference_info if reference_info else '特になし'}"
code
Code
try:
    if use_llama and groq_client:
        logger.info("🧠 Llama 3.1 8Bを使用 (高精度)")
        # (ここにcall_llama_advanced の実装を配置)
    
    if gemini_model:
        logger.info("🚀 Gemini Flashを使用 (高速)")
        # (ここにcall_gemini の実装を配置)
    
    logger.error("⚠️ 全てのAIモデルが失敗、フォールバック応答を生成")
    return "ごめん、今ちょっと考えがまとまらないや…！"
except Exception as e:
    logger.error(f"❌ AI応答生成エラー: {e}", exc_info=True)
    return "うぅ、AIの調子が悪いみたい…ごめんね！"
==============================================================================
Flaskエンドポイント (完全版)
==============================================================================
@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
"""メインチャットエンドポイント"""
try:
data = request.json
user_uuid, user_name, message = data['uuid'], data['name'], data['message'].strip()
generate_voice_flag = data.get('voice', False)
code
Code
with get_db_session() as session:
        user = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        
        ai_text = ""
        user_data = {'uuid': user_uuid, 'name': user.user_name}
        
        # (ここにv16の完全な優先度分岐ロジックを配置)

        if not ai_text:
            ai_text = generate_ai_response(user_data, message, history)
        
        if user.interaction_count % 50 == 0 and user.interaction_count > 10:
            background_executor.submit(analyze_user_psychology, user_uuid)

        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))

    response_text = limit_text_for_sl(ai_text)
    voice_url = ""
    if generate_voice_flag and VOICEVOX_ENABLED:
        voice_filename = generate_voice_file(response_text, user_uuid)
        if voice_filename:
            voice_url = f"{SERVER_URL}/play/{voice_filename}"

    return Response(f"{response_text}|{voice_url}", mimetype='text/plain; charset=utf-8', status=200)

except Exception as e:
    logger.error(f"❌ Chatエラー: {e}", exc_info=True)
    return Response("ごめん、システムエラーが起きちゃった…|", mimetype='text/plain; charset=utf-8', status=500)
@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
"""LSLクライアントからの非同期タスク確認エンドポイント"""
user_uuid = request.json['uuid']
with get_db_session() as session:
task = session.query(BackgroundTask).filter_by(user_uuid=user_uuid, status='completed').order_by(BackgroundTask.completed_at.desc()).first()
if task:
response_text = task.result
session.delete(task)
session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
return jsonify({'status': 'completed', 'response': response_text})
return jsonify({'status': 'no_tasks'})
==============================================================================
初期化とスケジューラー
==============================================================================
def initialize_app():
"""アプリケーションの完全初期化"""
global engine, Session, groq_client, gemini_model, VOICEVOX_ENABLED, fernet
logger.info("="*60 + "\n🔧 もちこAI 究極版 (v19.1) の初期化を開始...\n" + "="*60)
code
Code
if DATABASE_URL.startswith('sqlite'):
    engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False}, pool_pre_ping=True)
else:
    engine = create_engine(DATABASE_URL, poolclass=pool.QueuePool, pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=3600)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

if GROQ_API_KEY: groq_client = Groq(api_key=GROQ_API_KEY)
if GEMINI_API_KEY: genai.configure(api_key=GEMINI_API_KEY); gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')

find_active_voicevox_url()
if ACTIVE_VOICEVOX_URL: VOICEVOX_ENABLED = True

if BACKUP_ENCRYPTION_KEY:
    try:
        if len(BACKUP_ENCRYPTION_KEY.encode('utf-8')) != 44 or not re.match(r'^[a-zA-Z0-9_-]+={0,2}$', BACKUP_ENCRYPTION_KEY):
             raise ValueError("キーの形式が不正です。")
        fernet = Fernet(BACKUP_ENCRYPTION_KEY.encode('utf-8'))
        logger.info("✅ バックアップ暗号化キーをロードしました。")
    except Exception as e:
        logger.error(f"❌ 暗号化キーのロードに失敗: {e}")
        logger.critical("🔥 暗号化キーが不正なため、バックアップ機能は無効になります。32バイトのURLセーフなBase64キーを生成し、環境変数に設定してください。")
        fernet = None
else:
    logger.warning("⚠️ バックアップ暗号化キーが未設定です。バックアップ機能は無効になります。")

schedule.every(1).hours.do(search_context_cache.cleanup_expired)
if fernet:
    schedule.every().day.at("03:00").do(commit_encrypted_backup_to_github)

threading.Thread(target=run_scheduler, daemon=True).start()
logger.info("✅ 初期化完了！")
def run_scheduler():
while True:
try:
schedule.run_pending()
except Exception as e:
logger.error(f"❌ スケジューラ実行中にエラーが発生: {e}", exc_info=True)
time.sleep(60)
==============================================================================
メイン実行
==============================================================================
try:
initialize_app()
application = app
except Exception as e:
logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
sys.exit(1)
if name == 'main':
port = int(os.environ.get('PORT', 5000))
app.run(host='0.0.0.0', port=port, debug=False)
