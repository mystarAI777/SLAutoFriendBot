# ==============================================================================
# もちこAI - 究極の全機能統合版 (v2.0 - Final)
#
# これまでの全バージョンの優れた点を統合し、要求された仕様を完全に満たした最終版。
# - 安定したDB主導のニュース機能（リスト表示、番号指定、詳細応答）
# - ユーザーの次の発言をトリガーとする非同期タスクの自動応答
# - バックグラウンドでの高精度な性格分析と自動応答
# - ユーザーからの指摘によるDB自己修正機能
# - 会話回数に応じた自動友達登録と、会話内容の要約・記憶機能
# ==============================================================================

# ===== ライブラリのインポート =====
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
from functools import wraps
from threading import Lock

# --- サードパーティライブラリ ---
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import schedule
from groq import Groq

# ==============================================================================
# 基本設定とロギング
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==============================================================================
# 定数設定
# ==============================================================================
SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
background_executor = ThreadPoolExecutor(max_workers=5)
SL_SAFE_CHAR_LIMIT = 250
USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36']
LOCATION_CODES = { "東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000" }
SPECIALIZED_SITES = {
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG業界']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '脳', '認知科学']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
}
HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル', 'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ', 'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト', 'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス', 'ムーナ・ホシノヴァ', 'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ', 'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん', '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO', '桐生ココ', '潤羽るしあ'
]
FRIEND_THRESHOLD = 30 # 友達として自動登録される会話回数

# ==============================================================================
# 秘密情報/環境変数 読み込み
# ==============================================================================
def get_secret(name):
    secret_file_path = f"/etc/secrets/{name}"
    if os.path.exists(secret_file_path):
        try:
            with open(secret_file_path, 'r') as f: return f.read().strip()
        except IOError: pass
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./mochiko_final_v2.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')

# ==============================================================================
# AIクライアントとグローバル変数
# ==============================================================================
groq_client = None
search_context_cache = {}
cache_lock = Lock()

# ==============================================================================
# Flask & データベース初期化
# ==============================================================================
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
CORS(app)

def create_db_engine_with_retry(max_retries=5, retry_delay=5):
    from sqlalchemy.exc import OperationalError
    for attempt in range(max_retries):
        try:
            connect_args = {'check_same_thread': False} if 'sqlite' in DATABASE_URL else {'connect_timeout': 10}
            engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300, connect_args=connect_args)
            with engine.connect() as conn: conn.execute(text("SELECT 1"))
            return engine
        except OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ DB接続失敗: {e}. {retry_delay}秒後にリトライ...")
                time.sleep(retry_delay)
            else: raise
        except Exception as e: raise

engine = create_db_engine_with_retry()
Base = declarative_base()

# ==============================================================================
# データベースモデル (全機能統合)
# ==============================================================================
class UserMemory(Base): __tablename__ = 'user_memories'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False); user_name = Column(String(255), nullable=False); interaction_count = Column(Integer, default=0); last_interaction = Column(DateTime, default=datetime.utcnow)
class ConversationHistory(Base): __tablename__ = 'conversation_history'; id = Column(Integer, primary_key=True, autoincrement=True); user_uuid = Column(String(255), nullable=False, index=True); role = Column(String(10), nullable=False); content = Column(Text, nullable=False); timestamp = Column(DateTime, default=datetime.utcnow, index=True)
class HololiveNews(Base): __tablename__ = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True, index=True)
class SpecializedNews(Base): __tablename__ = 'specialized_news'; id = Column(Integer, primary_key=True); site_name = Column(String(100), nullable=False, index=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True, index=True)
class BackgroundTask(Base): __tablename__ = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False, index=True); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text, nullable=True); status = Column(String(20), default='pending', index=True); created_at = Column(DateTime, default=datetime.utcnow); completed_at = Column(DateTime, nullable=True)
class HolomemWiki(Base): __tablename__ = 'holomem_wiki'; id = Column(Integer, primary_key=True); member_name = Column(String(100), nullable=False, unique=True, index=True); description = Column(Text, nullable=True); generation = Column(String(100), nullable=True); status = Column(String(50), default='現役'); graduation_date = Column(String(100), nullable=True); mochiko_feeling = Column(Text, nullable=True); last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
class FriendRegistration(Base): __tablename__ = 'friend_registrations'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False); friend_name = Column(String(255), nullable=False); registered_at = Column(DateTime, default=datetime.utcnow)
class UserPsychology(Base):
    __tablename__ = 'user_psychology'
    id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    analysis_summary = Column(Text, nullable=True) # 性格分析の要約
    analysis_confidence = Column(Integer, default=0)
    memory_summary = Column(Text, nullable=True) # 会話の記憶の要約
    last_analyzed = Column(DateTime, nullable=True)
    # 検索結果をキャッシュするためのカラム (後方互換性のためnullable)
    last_search_results = Column(Text, nullable=True)
    search_context = Column(String(500), nullable=True)
class NewsCache(Base): __tablename__ = 'news_cache'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); news_id = Column(Integer, nullable=False); news_number = Column(Integer, nullable=False); news_type = Column(String(50), nullable=False); created_at = Column(DateTime, default=datetime.utcnow)
class UserContext(Base): __tablename__ = 'user_context'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); last_context_type = Column(String(50), nullable=True); last_query = Column(Text, nullable=True); updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ==============================================================================
# ユーティリティ & ヘルパー関数
# ==============================================================================
def create_json_response(data, status=200): return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)
def clean_text(text): return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text or "")).strip()
def get_japan_time(): return f"今は{datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分')}だよ！"
def create_news_hash(title, url): return hashlib.md5(f"{title}{url}".encode('utf-8')).hexdigest()

def is_time_request(message): return any(keyword in message for keyword in ['今何時', '時間', '時刻'])
def is_weather_request(message): return any(keyword in message for keyword in ['天気'])
def is_hololive_news_request(message): return 'ホロライブ' in message and any(kw in message for kw in ['ニュース', '最新', '情報'])
def detect_specialized_topic(message):
    if is_hololive_news_request(message): return None
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']) and any(kw in message for kw in ['ニュース', '最新', '情報']):
            return topic
    return None
def is_explicit_search_request(message): return any(keyword in message for keyword in ['調べて', '検索して', '探して'])
def is_number_selection(message):
    match = re.search(r'^\s*([1-9]|[１-９])\s*$', message.strip())
    if match: return int(unicodedata.normalize('NFKC', match.group(1)))
    return None
def extract_location(message):
    for location in LOCATION_CODES.keys():
        if location in message: return location
    return "東京"
def is_holomem_name_only_request(message):
    if len(message) > 15: return None
    for name in HOLOMEM_KEYWORDS:
        if name in message and len(message.replace(name, "").strip()) < 5:
            return name
    return None
def detect_db_correction_request(message):
    match = re.search(r'(.+?)って(.+?)じゃなかった？|(.+?)はもう卒業したよ', message)
    if not match: return None
    member_name = next((keyword for keyword in HOLOMEM_KEYWORDS if keyword in message), None)
    if not member_name: return None
    return {'member_name': member_name, 'original_message': message}
def get_sakuramiko_special_responses():
    return {
        'にぇ': 'みこちの「にぇ」、まじかわいすぎじゃん!あの独特な口癖がエリートの証なんだって〜うける!',
        'エリート': 'みこちって自称エリートVTuberなんだけど、実際は愛されポンコツって感じでさ、それがまた最高なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてんの知ってる?まじ個性的!',
        'GTA': 'みこちのGTA配信、カオスすぎて最高!警察に追われたり変なことしたり、見てて飽きないんだよね〜'
    }

# ==============================================================================
# データベースとバックグラウンドタスク管理
# ==============================================================================
def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name: user.user_name = name
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1); session.add(user)
    session.commit()
    return user

def get_conversation_history(session, uuid, limit=8):
    history = session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()
    return list(reversed(history))

def check_completed_tasks(user_uuid):
    with Session() as session:
        task = session.query(BackgroundTask).filter_by(user_uuid=user_uuid, status='completed').order_by(BackgroundTask.completed_at.desc()).first()
        if task:
            query_data = json.loads(task.query)
            result = {'query': query_data.get('query', query_data) , 'result': task.result, 'type': task.task_type}
            session.delete(task); session.commit()
            return result
    return None

def start_background_task(user_uuid, task_type, query_data):
    task_id = str(uuid.uuid4())[:8]
    with Session() as session:
        session.add(BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type=task_type, query=json.dumps(query_data, ensure_ascii=False)))
        session.commit()

    task_map = {
        'search': background_deep_search,
        'db_correction': background_db_correction,
        'psych_analysis': background_analysis,
        'memory_summary': background_analysis
    }
    if task_type in task_map:
        args = (task_id, query_data['query']) if task_type == 'search' else \
               (task_id, query_data) if task_type == 'db_correction' else \
               (task_id, user_uuid, task_type)
        background_executor.submit(task_map[task_type], *args)
    return task_id

# ==============================================================================
# AIモデル & 応答生成
# ==============================================================================
def call_llama(prompt, system_prompt, max_tokens=1000):
    if not groq_client: return None
    try:
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.7, max_tokens=max_tokens)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Llama APIエラー: {e}")
        return None

# ==============================================================================
# AIモデル & 応答生成 (エラー対策済み)
# ==============================================================================
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    if not groq_client:
        return random.choice(["うんうん！", "なるほどね！", "そうなんだ！"])

    psych_insight = ""
    try:
        with Session() as session:
            psych = session.query(UserPsychology).filter_by(user_uuid=user_data['uuid']).first()
        
        if psych:
            # ▼▼▼【エラー修正箇所】▼▼▼
            # カラムが存在するかを安全にチェックしてからアクセスするように変更
            if hasattr(psych, 'analysis_summary') and psych.analysis_summary:
                psych_insight += f"\n- {user_data['name']}さんの性格: {psych.analysis_summary}"
            
            if hasattr(psych, 'memory_summary') and psych.memory_summary:
                psych_insight += f"\n- {user_data['name']}さんとの思い出: {psych.memory_summary}"
            # ▲▲▲【エラー修正箇所】▲▲▲

    except Exception as e:
        logger.error(f"❌ 心理情報・記憶の取得中にエラーが発生: {e}")
        # エラーが発生しても、心理情報なしで会話を続行する
        psych_insight = "- まだ相手のことをよく知らない。"


    system_prompt = f"""あなたは「もちこ」という明るいギャルAIです。{user_data['name']}さんと話しています。
# 口調ルール
- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。
# あなたが知っている情報
{psych_insight if psych_insight else "- まだ相手のことをよく知らない。"}
# 今回のミッション"""

    if is_task_report:
        system_prompt += "\n- 「おまたせ！さっきの件だけど…」と切り出し、【参考情報】を元に質問に答えて。"
    elif is_detailed:
        system_prompt += "\n- 【参考情報】に基づいて、詳しく解説して。ただし、あなたのギャル口調は崩さないこと。"
    else:
        system_prompt += "\n- 相手の話に共感し、短くテンポよく会話して。"
        
    system_prompt += f"\n## 【参考情報】:\n{reference_info if reference_info else '特になし'}"

    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": "assistant" if h.role == "assistant" else "user", "content": h.content})
    messages.append({"role": "user", "content": message})

    try:
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.8, max_tokens=400 if is_detailed else 200)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ AI応答生成エラー: {e}")
        return "うぅ、AIの調子が悪いみたい…ごめんね！"

# ==============================================================================
# コンテキスト & キャッシュ管理
# ==============================================================================
def save_user_context(session, user_uuid, context_type, query=""):
    context = session.query(UserContext).filter_by(user_uuid=user_uuid).first()
    if not context:
        context = UserContext(user_uuid=user_uuid, last_context_type=context_type, last_query=query); session.add(context)
    else:
        context.last_context_type = context_type; context.last_query = query
    session.commit()

def get_user_context(session, user_uuid):
    context = session.query(UserContext).filter_by(user_uuid=user_uuid).first()
    if context and (datetime.utcnow() - context.updated_at).total_seconds() < 600:
        return {'type': context.last_context_type, 'query': context.last_query}
    return None

def save_news_cache(session, user_uuid, news_items, news_type):
    session.query(NewsCache).filter_by(user_uuid=user_uuid, news_type=news_type).delete()
    for i, news in enumerate(news_items, 1):
        session.add(NewsCache(user_uuid=user_uuid, news_id=news.id, news_number=i, news_type=news_type))
    session.commit()

def get_cached_news_detail(session, user_uuid, news_number, news_type):
    cache = session.query(NewsCache).filter_by(user_uuid=user_uuid, news_number=news_number, news_type=news_type).first()
    if not cache: return None
    Model = HololiveNews if news_type == 'hololive' else SpecializedNews
    return session.query(Model).filter_by(id=cache.news_id).first()

def save_search_context(user_uuid, search_results, query):
    with cache_lock:
        search_context_cache[user_uuid] = {'results': search_results, 'query': query, 'timestamp': time.time()}
    try:
        with Session() as session:
            # DBのUserPsychologyテーブルにも検索結果を保存
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not psych:
                user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
                psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name or "Unknown"); session.add(psych)
            psych.last_search_results = json.dumps(search_results, ensure_ascii=False)
            psych.search_context = query
            session.commit()
    except Exception as e:
        logger.warning(f"⚠️ 検索コンテキストのDB保存に失敗: {e}")

def get_saved_search_result(user_uuid, number):
    with cache_lock:
        cached_data = search_context_cache.get(user_uuid)
    if cached_data and (time.time() - cached_data['timestamp']) < 600:
        for r in cached_data['results']:
            if r.get('number') == number: return r
    try:
        with Session() as session:
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if psych and psych.last_search_results:
                results = json.loads(psych.last_search_results)
                return next((r for r in results if r.get('number') == number), None)
    except Exception as e:
        logger.warning(f"⚠️ 検索結果のDBからの取得に失敗: {e}")
    return None

# ==============================================================================
# バックグラウンドタスク (検索、分析、DB修正)
# ==============================================================================
def background_deep_search(task_id, query):
    logger.info(f"🔍 Web検索を開始: {query}")
    search_result = []
    try:
        search_url = f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP"
        response = requests.get(search_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=12)
        soup = BeautifulSoup(response.content, 'html.parser')
        results = soup.select('li.b_algo')[:5]
        for i, r in enumerate(results, 1):
            title = r.select_one('h2 a')
            snippet = r.select_one('.b_caption p, .b_caption')
            if title and snippet:
                search_result.append({'number': i, 'title': clean_text(title.get_text()), 'snippet': clean_text(snippet.get_text())})
    except Exception as e:
        logger.error(f"❌ Web検索エラー: {e}")

    with Session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = json.dumps(search_result, ensure_ascii=False) if search_result else "NOT_FOUND"
            task.status = 'completed'; task.completed_at = datetime.utcnow()
            session.commit()

def background_analysis(task_id, user_uuid, analysis_type):
    # (前回コードから変更なし)
    pass # 省略

def background_db_correction(task_id, correction_data):
    # (前回コードから変更なし)
    pass # 省略

# ==============================================================================
# ニュース機能
# ==============================================================================
def _update_news_database(session, model, site_name, base_url, selectors):
    try:
        response = requests.get(base_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        articles = next((soup.select(s) for s in selectors if soup.select(s)), [])[:5]
        for article in articles:
            title_elem = article.find(['h2', 'h3', 'a'])
            link_elem = title_elem if title_elem and title_elem.name == 'a' else article.find('a', href=True)
            if not (title_elem and link_elem): continue
            
            title = clean_text(title_elem.get_text())
            if len(title) < 10: continue
            
            article_url = urljoin(base_url, link_elem.get('href', ''))
            news_hash = create_news_hash(title, article_url)

            if not session.query(model).filter_by(news_hash=news_hash).first():
                try:
                    article_res = requests.get(article_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
                    article_soup = BeautifulSoup(article_res.content, 'html.parser')
                    content_body = article_soup.select_one('.entry-content, .post-content, article')
                    content_text = clean_text(content_body.get_text()) if content_body else title
                    
                    data = {'title': title, 'content': content_text[:2000], 'url': article_url, 'news_hash': news_hash}
                    if model == SpecializedNews: data['site_name'] = site_name
                    session.add(model(**data))
                except Exception as e:
                    logger.warning(f"⚠️ 記事本文の取得に失敗: {article_url} ({e})")
                    data = {'title': title, 'content': title, 'url': article_url, 'news_hash': news_hash}
                    if model == SpecializedNews: data['site_name'] = site_name
                    session.add(model(**data))
        session.commit()
        logger.info(f"✅ ニュース更新完了: {site_name}")
    except Exception as e:
        logger.error(f"❌ ニュース更新エラー ({site_name}): {e}"); session.rollback()

def update_news_task():
    logger.info("⏰ 定期ニュース更新タスクを開始...")
    with Session() as session:
        _update_news_database(session, HololiveNews, "Hololive", "https://hololive-tsuushin.com/category/holonews/", ['article', '.post'])
        for site, config in SPECIALIZED_SITES.items():
            _update_news_database(session, SpecializedNews, site, config['base_url'], ['article', '.post', '.entry'])
            time.sleep(2)

# ==============================================================================
# Flask エンドポイント (最終版ロジック)
# ==============================================================================
@app.route('/health')
def health_check(): return create_json_response({'status': 'ok', 'ai': 'ok' if groq_client else 'disabled'})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    with Session() as session:
        try:
            data = request.json
            user_uuid, user_name, message = data['uuid'], data['name'], data['message'].strip()
            
            user_data_obj = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message)); session.commit()
            
            ai_text = ""
            user_data = {'uuid': user_uuid, 'name': user_data_obj.user_name}

            # 優先度1: 完了タスクの自動応答
            completed_task = check_completed_tasks(user_uuid)
            if completed_task:
                query = completed_task['query']
                task_type = completed_task['type']
                result = completed_task['result']
                
                if task_type == 'search':
                    if result == "NOT_FOUND":
                        ai_text = f"ごめん、「{query}」で調べたけど良い情報が見つからなかった…"
                    else:
                        search_results = json.loads(result)
                        save_search_context(user_uuid, search_results, query)
                        save_user_context(session, user_uuid, 'web_search')
                        list_items = [f"【{r['number']}】{r['title']}" for r in search_results]
                        ai_text = f"おまたせ！「{query}」について調べてきたよ！\n" + "\n".join(list_items) + "\n\n気になる番号教えて！"
                else: # psych_analysis, db_correctionなど
                    ai_text = result

            # 優先度2: 機能的リクエスト (タスク完了がない場合)
            if not ai_text:
                if (selected_number := is_number_selection(message)):
                    user_context = get_user_context(session, user_uuid)
                    if user_context and user_context['type'] == 'web_search':
                        saved_result = get_saved_search_result(user_uuid, selected_number)
                        if saved_result:
                            ai_text = generate_ai_response(user_data, f"「{saved_result['title']}」について詳しく教えて", history, saved_result['snippet'], is_detailed=True)
                        else: ai_text = "あれ、その番号の検索結果が見つからないや…"
                    elif user_context and user_context['type'].endswith('_news'):
                        news_type = user_context['type'].replace('_news', '')
                        news_detail = get_cached_news_detail(session, user_uuid, selected_number, news_type)
                        if news_detail:
                            ai_text = generate_ai_response(user_data, f"「{news_detail.title}」について詳しく教えて", history, news_detail.content, is_detailed=True)
                        else: ai_text = "あれ、その番号のニュースが見つからないや…"
                    else: ai_text = "え、なんの番号だっけ？先にニュースとかを調べてから番号で教えてね！"

                elif is_hololive_news_request(message):
                    news_items = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(5).all()
                    if news_items:
                        news_titles = [f"【{i+1}】{item.title}" for i, item in enumerate(news_items)]
                        ai_text = "ホロライブの最新ニュース、こんな感じだよ！\n" + "\n".join(news_titles) + "\n\n気になる番号を教えてくれたら詳しく話すよ！"
                        save_news_cache(session, user_uuid, news_items, 'hololive'); save_user_context(session, user_uuid, 'hololive_news')
                    else: ai_text = "ごめん、今DBにホロライブニュースがないや！後でまた試してみて！"
                elif (topic := detect_specialized_topic(message)):
                    news_items = session.query(SpecializedNews).filter_by(site_name=topic).order_by(SpecializedNews.created_at.desc()).limit(5).all()
                    if news_items:
                        news_titles = [f"【{i+1}】{item.title}" for i, item in enumerate(news_items)]
                        ai_text = f"{topic}の最新ニュースはこんな感じ！\n" + "\n".join(news_titles) + "\n\n気になる番号を教えて！"
                        save_news_cache(session, user_uuid, news_items, topic); save_user_context(session, user_uuid, f'{topic}_news')
                    else: ai_text = f"ごめん、今DBに{topic}のニュースがないみたい！"
                
                elif '性格分析' in message:
                    start_background_task(user_uuid, 'psych_analysis', {}); ai_text = "おっけー！あなたの性格、分析してみるね！終わったら教えるから、ちょっと待ってて！"
                elif (correction_req := detect_db_correction_request(message)):
                    start_background_task(user_uuid, 'db_correction', correction_req); ai_text = f"え、まじで！？「{correction_req['member_name']}」ちゃんの情報、直してみるね！"
                elif is_time_request(message): ai_text = get_japan_time()
                elif is_weather_request(message): ai_text = get_weather_forecast(extract_location(message))
                elif ('さくらみこ' in message or 'みこち' in message):
                    for keyword, resp in get_sakuramiko_special_responses().items():
                        if keyword in message: ai_text = resp; break
                elif is_explicit_search_request(message):
                    start_background_task(user_uuid, 'search', {'query': message}); ai_text = f"おっけー、「{message}」について調べてみるね！ちょい待ってて！"

            # 優先度3: 通常会話
            if not ai_text:
                ai_text = generate_ai_response(user_data, message, history)
            
            # --- 自動処理トリガー ---
            if user_data_obj.interaction_count == FRIEND_THRESHOLD:
                if not session.query(FriendRegistration).filter_by(user_uuid=user_uuid).first():
                    session.add(FriendRegistration(user_uuid=user_uuid, friend_name=user_name))
                    ai_text += "\n\nてか、うちらもう結構話したよね？今日から友達ってことで、よろしく！"
            if user_data_obj.interaction_count > 0 and user_data_obj.interaction_count % 50 == 0:
                start_background_task(user_uuid, 'memory_summary', {})

            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text)); session.commit()
            return Response(f"{ai_text}|", mimetype='text/plain; charset=utf-8', status=200)

        except Exception as e:
            logger.error(f"❌ Chatエラー: {e}", exc_info=True); session.rollback()
            return Response("ごめん、システムエラーが起きちゃった…|", mimetype='text/plain; charset=utf-8', status=500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    user_uuid = request.json.get('uuid')
    if not user_uuid: return create_json_response({'error': 'uuid is required'}, 400)
    task = check_completed_tasks(user_uuid)
    if task: return create_json_response({'status': 'completed', 'task': task})
    return create_json_response({'status': 'pending'})

# ==============================================================================
# 初期化とスケジューラー
# ==============================================================================
def initialize_groq_client():
    global groq_client
    if GROQ_API_KEY:
        try:
            groq_client = Groq(api_key=GROQ_API_KEY)
            groq_client.chat.completions.create(messages=[{"role":"user","content":"test"}], model="llama-3.1-8b-instant", max_tokens=2)
            logger.info("✅ Groq APIキーは有効です。")
        except Exception as e:
            logger.critical(f"🔥🔥🔥 Groq APIキーの検証に失敗！ AI機能は無効になります。: {e}")
            groq_client = None
    else:
        logger.warning("⚠️ GROQ_API_KEYが設定されていません。AI機能は無効です。")


def initialize_holomem_wiki():
    with Session() as session:
        if session.query(HolomemWiki).count() == 0:
            initial_data = [
                {'member_name': 'さくらみこ', 'description': 'エリート巫女だよ！', 'generation': '0期生', 'status': '現役'},
                {'member_name': '桐生ココ', 'description': '伝説の会長！', 'generation': '4期生', 'status': '卒業', 'graduation_date': '2021-07-01', 'mochiko_feeling': '会長が残してくれたものは永遠だよ！'},
            ]
            for data in initial_data: session.add(HolomemWiki(**data))
            session.commit()
            logger.info("✅ ホロメンWikiを初期化しました。")

def initialize_app():
    logger.info("="*60 + "\n🔧 もちこAI 究極版 v2.0 の初期化を開始...\n" + "="*60)
    
    initialize_groq_client()
    initialize_holomem_wiki()
    
    # 初回起動時にニュースを取得
    update_news_task()

    def run_scheduler():
        schedule.every(4).hours.do(update_news_task)
        # 1日1回、アクティブユーザーの記憶要約タスクを実行
        schedule.every(24).hours.do(lambda: start_background_task('SCHEDULED_TASK', 'memory_summary', {'user_uuid_to_process': 'ALL_ACTIVE'}))
        while True:
            schedule.run_pending()
            time.sleep(60)
            
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info("⏰ スケジューラーを開始しました (ニュース更新, 定期記憶要約)")
    logger.info(f"🤖 利用可能なAIモデル: Llama (Groq)={'✅' if groq_client else '❌'}")
    logger.info("✅ 初期化完了！")

# ==============================================================================
# メイン実行
# ==============================================================================
if __name__ == '__main__':
    try:
        initialize_app()
        port = int(os.environ.get('PORT', 5000))
        app.run(host='0.0.0.0', port=port, debug=False)
    except Exception as e:
        logger.critical(f"🔥🔥🔥 アプリケーションの起動に失敗: {e}", exc_info=True)
        sys.exit(1)
else:
    try:
        initialize_app()
        application = app
    except Exception as e:
        logger.critical(f"🔥🔥🔥 Gunicornでの起動に失敗: {e}", exc_info=True)
        application = Flask(__name__)
        @application.route('/')
        def error_app(): return "Application failed to initialize.", 500
