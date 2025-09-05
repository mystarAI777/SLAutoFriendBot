import os
import requests
import logging
import sys
import time
import threading
import json
import re
import sqlite3
import random
import uuid
from datetime import datetime, timedelta
from typing import Union, Dict, Any, List
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import asyncio
from threading import Lock
import schedule

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 設定定数
VOICEVOX_MAX_TEXT_LENGTH = 30
VOICEVOX_FAST_TIMEOUT = 3
CONVERSATION_HISTORY_TURNS = 2
VOICE_DIR = '/tmp/voices'
SERVER_URL = "https://slautofriendbot.onrender.com"

# バックグラウンドタスク管理
background_executor = ThreadPoolExecutor(max_workers=5)

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name: str) -> Union[str, None]:
    env_value = os.environ.get(name)
    if env_value:
        return env_value
    try:
        with open(f'/etc/secrets/{name}', 'r') as f:
            return f.read().strip()
    except Exception:
        return None

DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
WEATHER_API_KEY = get_secret('WEATHER_API_KEY')

# --- クライアント初期化 ---
groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groqクライアント初期化成功")
    except Exception as e:
        logger.error(f"❌ Groqクライアント初期化エラー: {e}")

# VOICEVOX設定
VOICEVOX_URLS = ['http://localhost:50021', 'http://127.0.0.1:50021', 'http://voicevox-engine:50021', 'http://voicevox:50021']
WORKING_VOICEVOX_URL = VOICEVOX_URL_FROM_ENV or VOICEVOX_URLS[0]
VOICEVOX_ENABLED = True

# 必須設定チェック
if not all([DATABASE_URL, groq_client]):
    logger.critical("FATAL: 必須設定(DATABASE_URL or GROQ_API_KEY)が不足しています。")
    sys.exit(1)

# Flask & データベース初期化
app = Flask(__name__)
CORS(app)
engine = create_engine(DATABASE_URL)
Base = declarative_base()

# --- データベースモデル ---
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
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

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000))
    published_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    news_hash = Column(String(100), unique=True)

class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False)
    task_type = Column(String(50), nullable=False)
    query = Column(Text, nullable=False)
    result = Column(Text)
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# --- 専門サイト設定 (追加) ---
SPECIALIZED_SITES = {
    'Blender': {
        'base_url': 'https://docs.blender.org/manual/ja/latest/',
        'keywords': ['Blender', 'ブレンダー', '3Dモデリング']
    },
    'CGニュース': {
        'base_url': 'https://modelinghappy.com/',
        'keywords': ['CGニュース', '3DCG', 'レンダリング']
    },
    '脳科学・心理学': {
        'base_url': 'https://nazology.kusuguru.co.jp/',
        'keywords': ['脳科学', '心理学', '認知科学']
    },
    'セカンドライフ': {
        'base_url': 'https://community.secondlife.com/news/',
        'keywords': ['セカンドライフ', 'Second Life', 'SL']
    }
}
ALL_SPECIALIZED_KEYWORDS = [keyword for site in SPECIALIZED_SITES.values() for keyword in site['keywords']]


# --- ホロライブ関連設定 ---
HOLOLIVE_NEWS_URL = "https://hololive.hololivepro.com/news"
HOLOLIVE_WIKI_BASE = "https://seesaawiki.jp/hololivetv/"
HOLOMEM_KEYWORDS = [
    'ホロライブ', 'ホロメン', 'hololive', 'VTuber', 'ときのそら', 'ロボ子', 'さくらみこ', '星街すいせい', 'AZKi',
    '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね',
    '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ',
    '雪花ラミィ', '尾丸ポルカ', '桃鈴ねね', '獅白ぼたん', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは',
    '森美声', 'カリオペ', 'ワトソン', 'アメリア', 'がうる・ぐら', 'ホロライブプロダクション', 'カバー株式会社', 'YAGOO', '谷郷元昭',
    'ホロフェス', 'ホロライブエラー', 'ホロライブオルタナティブ'
]

# --- ユーティリティ関数 ---
def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_japan_time() -> str:
    jst = datetime.timezone(timedelta(hours=9))
    now = datetime.now(jst)
    weekdays = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']
    weekday = weekdays[now.weekday()]
    return f"今は{now.year}年{now.month}月{now.day}日({weekday})の{now.hour}時{now.minute}分だよ！"

def create_news_hash(title: str, content: str) -> str:
    import hashlib
    combined = f"{title}{content[:100]}"
    return hashlib.md5(combined.encode('utf-8')).hexdigest()

# --- 判定関数 ---
def is_time_request(message: str) -> bool:
    return any(keyword in message for keyword in ['今何時', '時間', '時刻'])

def is_weather_request(message: str) -> bool:
    return any(keyword in message for keyword in ['天気', 'てんき', '気温'])

def is_hololive_request(message: str) -> bool:
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

# (修正) 専門サイトのキーワードも検索対象に
def should_search(message: str) -> bool:
    search_patterns = [r'(?:とは|について|教えて|知りたい)', r'(?:最新|今日|ニュース)', r'(?:調べて|検索)']
    search_words = ['誰', '何', 'どこ', 'いつ', 'どうして', 'なぜ']
    if any(re.search(pattern, message) for pattern in search_patterns): return True
    if any(word in message for word in search_words): return True
    if any(keyword in message for keyword in ALL_SPECIALIZED_KEYWORDS): return True
    return False

def is_short_response(message: str) -> bool:
    short_responses = ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'へー', 'ふーん']
    return len(message.strip()) <= 3 or message.strip() in short_responses

# (追加) 専門トピック検出
def detect_specialized_topic(message: str) -> Union[str, None]:
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']):
            return topic
    return None


# --- 天気取得機能 ---
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}
def extract_location(message: str) -> str:
    for location in LOCATION_CODES.keys():
        if location in message: return location
    return "東京"

def get_weather_forecast(location: str) -> Union[str, None]:
    area_code = LOCATION_CODES.get(location)
    if not area_code: return None
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        return f"{location}の天気だよ！{clean_text(data.get('text', ''))[:50]}..."
    except Exception as e:
        logger.error(f"天気API取得エラー: {e}")
        return None

# --- ホロライブ情報取得機能 ---
def scrape_hololive_news() -> List[Dict[str, str]]:
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(HOLOLIVE_NEWS_URL, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        news_items = []
        for article in soup.find_all('article', limit=10):
            try:
                title_elem = article.find(['h1', 'h2', 'h3', 'h4'])
                if title_elem:
                    title = clean_text(title_elem.get_text())
                    content_elem = article.find(['p', 'div'], class_=re.compile(r'(content|text)'))
                    content = clean_text(content_elem.get_text()) if content_elem else title
                    link_elem = article.find('a')
                    url = urljoin(HOLOLIVE_NEWS_URL, link_elem.get('href')) if link_elem and link_elem.get('href').startswith('/') else link_elem.get('href')
                    if title and len(title) > 5:
                        news_items.append({'title': title, 'content': content[:500], 'url': url, 'published_date': datetime.utcnow()})
            except Exception as e:
                logger.error(f"記事解析エラー: {e}")
        logger.info(f"✅ ホロライブニュース取得: {len(news_items)}件")
        return news_items
    except Exception as e:
        logger.error(f"ホロライブニュース取得エラー: {e}")
        return []

def update_hololive_news_database():
    session = Session()
    try:
        news_items = scrape_hololive_news()
        added_count = 0
        for item in news_items:
            news_hash = create_news_hash(item['title'], item['content'])
            if not session.query(HololiveNews).filter_by(news_hash=news_hash).first():
                news = HololiveNews(title=item['title'], content=item['content'], url=item['url'], published_date=item['published_date'], news_hash=news_hash, created_at=datetime.utcnow())
                session.add(news)
                added_count += 1
        session.commit()
        if added_count > 0: logger.info(f"📰 ホロライブニュース更新: {added_count}件追加")
    except Exception as e:
        logger.error(f"ホロライブニュースDB更新エラー: {e}")
        session.rollback()
    finally:
        session.close()

def get_hololive_info_from_db(query: str = "") -> Union[str, None]:
    session = Session()
    try:
        q = session.query(HololiveNews)
        if query: q = q.filter(HololiveNews.title.contains(query) | HololiveNews.content.contains(query))
        news_list = q.order_by(HololiveNews.created_at.desc()).limit(3).all()
        if news_list:
            result = "最新のホロライブ情報だよ！\n"
            for news in news_list: result += f"・{news.title}: {news.content[:80]}...\n"
            return result[:200] + "..."
        return None
    except Exception as e:
        logger.error(f"ホロライブDB取得エラー: {e}")
        return None
    finally:
        session.close()

# --- 検索エンジン機能 ---
USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36']
def get_random_user_agent(): return random.choice(USER_AGENTS)

def search_hololive_wiki(query: str) -> Union[str, None]:
    try:
        search_url = f"{HOLOLIVE_WIKI_BASE}d/search?keywords={quote_plus(query)}"
        headers = {'User-Agent': get_random_user_agent()}
        response = requests.get(search_url, headers=headers, timeout=8)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        result_items = soup.find_all(['div', 'article'], class_=re.compile(r'(result|search|content)'))
        for item in result_items[:3]:
            text_content = clean_text(item.get_text())
            if text_content and len(text_content) > 20: return text_content[:150] + "..."
        return None
    except Exception as e:
        logger.error(f"ホロライブWiki検索エラー: {e}")
        return None

def quick_search(query: str) -> Union[str, None]:
    try:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {'User-Agent': get_random_user_agent()}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        result_div = soup.find('div', class_='links_main')
        if result_div and (snippet_elem := result_div.find('div', class_='result__snippet')):
            snippet = clean_text(snippet_elem.get_text())
            return snippet[:100] + "..." if len(snippet) > 100 else snippet
        return None
    except Exception as e:
        logger.error(f"軽量検索エラー: {e}")
        return None

# (追加) 専門サイト検索を実行する関数
def specialized_site_search(topic: str, query: str) -> Union[str, None]:
    if topic not in SPECIALIZED_SITES: return None
    config = SPECIALIZED_SITES[topic]
    # site: 演算子を使ってサイト内検索を実行
    site_query = f"site:{config['base_url']} {query}"
    logger.info(f"🔬 専門サイト内検索実行: {site_query}")
    return quick_search(site_query)


# --- バックグラウンド検索システム ---
# (修正) 専門サイト検索を組み込み
def background_deep_search(task_id: str, user_uuid: str, query: str):
    session = Session()
    try:
        logger.info(f"🔍 バックグラウンド検索開始: {query}")
        search_result = None

        # 1. 専門トピックを最優先で検索
        specialized_topic = detect_specialized_topic(query)
        if specialized_topic:
            search_result = specialized_site_search(specialized_topic, query)

        # 2. 専門トピックでない、または見つからなかった場合、ホロライブ関連を検索
        if not search_result and is_hololive_request(query):
            search_result = search_hololive_wiki(query)

        # 3. 上記で見つからない場合、通常のWeb検索
        if not search_result:
            search_result = quick_search(query)

        if search_result and groq_client:
            try:
                completion = groq_client.chat.completions.create(
                    messages=[{"role": "system", "content": f"以下の情報を「{query}」について30字以内で簡潔にまとめて：{search_result}"}],
                    model="llama-3.1-8b-instant", temperature=0.2, max_tokens=50)
                search_result = completion.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"AI要約エラー: {e}")

        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = search_result or "検索結果が見つからなかったよ..."
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
            logger.info(f"✅ バックグラウンド検索完了: {task_id}")
    except Exception as e:
        logger.error(f"バックグラウンド検索エラー: {e}")
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.status = 'failed'
            task.completed_at = datetime.utcnow()
            session.commit()
    finally:
        session.close()

def start_background_search(user_uuid: str, query: str) -> str:
    task_id = str(uuid.uuid4())[:8]
    session = Session()
    try:
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=query, status='pending')
        session.add(task)
        session.commit()
    finally:
        session.close()
    background_executor.submit(background_deep_search, task_id, user_uuid, query)
    return task_id

# --- 音声生成システム ---
def background_voice_generation(task_id: str, user_uuid: str, text: str):
    session = Session()
    try:
        if not VOICEVOX_ENABLED: return
        logger.info(f"🔊 バックグラウンド音声生成開始: {text[:20]}...")
        if len(text) > VOICEVOX_MAX_TEXT_LENGTH: text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
        query_response = requests.post(f"{WORKING_VOICEVOX_URL}/audio_query", params={"text": text, "speaker": 1}, timeout=VOICEVOX_FAST_TIMEOUT)
        query_response.raise_for_status()
        synthesis_response = requests.post(f"{WORKING_VOICEVOX_URL}/synthesis", params={"speaker": 1}, json=query_response.json(), timeout=VOICEVOX_FAST_TIMEOUT)
        synthesis_response.raise_for_status()
        filename = f"voice_{task_id}_{int(time.time())}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(synthesis_response.content)
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = f"/voice/{filename}"
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
        logger.info(f"✅ バックグラウンド音声生成完了: {filename}")
    except Exception as e:
        logger.error(f"バックグラウンド音声生成エラー: {e}")
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.status = 'failed'
            task.completed_at = datetime.utcnow()
            session.commit()
    finally:
        session.close()

def start_background_voice(user_uuid: str, text: str) -> str:
    task_id = str(uuid.uuid4())[:8]
    session = Session()
    try:
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='voice', query=text, status='pending')
        session.add(task)
        session.commit()
    finally:
        session.close()
    background_executor.submit(background_voice_generation, task_id, user_uuid, text)
    return task_id

# --- 完了タスク管理 ---
def check_completed_tasks(user_uuid: str) -> Dict[str, Any]:
    session = Session()
    try:
        result = {}
        completed_search = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == user_uuid, BackgroundTask.task_type == 'search', BackgroundTask.status == 'completed', BackgroundTask.completed_at > datetime.utcnow() - timedelta(minutes=5)).order_by(BackgroundTask.completed_at.desc()).first()
        if completed_search:
            result['search'] = {'query': completed_search.query, 'result': completed_search.result}
            session.delete(completed_search)
        completed_voice = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == user_uuid, BackgroundTask.task_type == 'voice', BackgroundTask.status == 'completed', BackgroundTask.completed_at > datetime.utcnow() - timedelta(minutes=2)).order_by(BackgroundTask.completed_at.desc()).first()
        if completed_voice:
            result['voice'] = completed_voice.result
            session.delete(completed_voice)
        session.commit()
        return result
    except Exception as e:
        logger.error(f"タスクチェックエラー: {e}")
        return {}
    finally:
        session.close()

# --- 高速AI応答生成 ---
def generate_quick_ai_response(user_data: Dict[str, Any], message: str, history: List[Any], completed_tasks: Dict[str, Any] = None) -> str:
    if not groq_client: return "今ちょっと調子悪いから、また話しかけて！"
    immediate_info = ""
    if is_time_request(message): immediate_info = get_japan_time()
    elif is_weather_request(message): immediate_info = get_weather_forecast(extract_location(message)) or ""
    elif is_hololive_request(message): immediate_info = get_hololive_info_from_db(message) or ""
    
    background_update = ""
    if completed_tasks and completed_tasks.get('search'):
        search_data = completed_tasks['search']
        background_update = f"そういえばさ、さっきの「{search_data['query']}」の話なんだけど、{search_data['result']}"
    
    system_prompt = f"""あなたは「もちこ」というギャルAIです。{user_data['name']}と話してます。
# ルール:
1. 自分は「あてぃし」、語尾に「〜じゃん」「〜だし」「〜的な？」使用
2. 「まじ」「てか」「やばい」「うける」使用
3. **返答は30字以内で短く！**
4. 丁寧語禁止
# 情報:
{immediate_info}
# バックグラウンド完了情報:
{background_update}
"""
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-2:]: messages.append({"role": h.role, "content": h.content})
    messages.append({"role": "user", "content": message})
    try:
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.8, max_tokens=60)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "ちょっと考え中...またすぐ話しかけて！"

# --- ユーザー管理 ---
def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name: user.user_name = name
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1, last_interaction=datetime.utcnow())
    session.add(user)
    session.commit()
    return {'uuid': user.user_uuid, 'name': user.user_name, 'interaction_count': user.interaction_count}

def get_conversation_history(session, uuid, turns=CONVERSATION_HISTORY_TURNS):
    return list(reversed(session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(turns * 2).all()))

# --- 初期化 ---
def check_voicevox_connection():
    global WORKING_VOICEVOX_URL, VOICEVOX_ENABLED
    for url in VOICEVOX_URLS:
        try:
            if requests.get(f"{url}/version", timeout=2).status_code == 200:
                WORKING_VOICEVOX_URL = url
                logger.info(f"✅ VOICEVOX接続成功: {url}")
                return True
        except: continue
    logger.warning("⚠️ VOICEVOX無効化")
    VOICEVOX_ENABLED = False
    return False

def initialize_voice_directory():
    global VOICE_DIR, VOICEVOX_ENABLED
    for candidate in ['/tmp/voices', '/app/voices', './voices', 'voices']:
        try:
            os.makedirs(candidate, exist_ok=True)
            test_file = os.path.join(candidate, 'test.tmp')
            with open(test_file, 'w') as f: f.write('test')
            os.remove(test_file)
            VOICE_DIR = candidate
            logger.info(f"✅ 音声ディレクトリ準備完了: {VOICE_DIR}")
            return
        except Exception: continue
    logger.warning("⚠️ 音声ディレクトリ作成失敗 - VOICEVOX無効化")
    VOICEVOX_ENABLED = False

def schedule_hololive_news_updates():
    schedule.every().hour.do(update_hololive_news_database)
    update_hololive_news_database()
    def run_schedule():
        while True:
            schedule.run_pending()
            time.sleep(60)
    threading.Thread(target=run_schedule, daemon=True).start()
    logger.info("📅 ホロライブニュース定期更新スケジュール開始（1時間毎）")

# --- Flask エンドポイント ---
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})

@app.route('/voice/<filename>')
def serve_voice_file(filename):
    try: return send_from_directory(VOICE_DIR, filename)
    except: return "File not found", 404

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json
        user_uuid, user_name, message = data.get('uuid'), data.get('name'), data.get('message', '').strip()
        if not all([user_uuid, user_name, message]): return "Error: uuid, name, message required", 400
        logger.info(f"💬 受信: {user_name} ({user_uuid[:8]}...): {message}")
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        completed_tasks = check_completed_tasks(user_uuid)
        ai_text = generate_quick_ai_response(user_data, message, history, completed_tasks)
        if should_search(message) and not is_short_response(message): start_background_search(user_uuid, message)
        if VOICEVOX_ENABLED: start_background_voice(user_uuid, ai_text)
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()
        audio_url = completed_tasks.get('voice', '')
        response_text = f"{ai_text}|{audio_url}"
        logger.info(f"💭 AI応答: {response_text}")
        return app.response_class(response=response_text, status=200, mimetype='text/plain; charset=utf-8')
    except Exception as e:
        logger.error(f"チャットエラー: {e}")
        return "Internal server error", 500
    finally:
        session.close()

@app.route('/api/status', methods=['GET'])
def api_status():
    session = Session()
    try:
        return jsonify({
            'version': '3.2.0-full-spec', 'status': 'active',
            'users': session.query(UserMemory).count(),
            'conversations': session.query(ConversationHistory).count(),
            'pending_tasks': session.query(BackgroundTask).filter_by(status='pending').count(),
            'hololive_news': session.query(HololiveNews).count(),
            'voicevox': VOICEVOX_ENABLED, 'fast_response': True,
        })
    except Exception as e: return jsonify({'error': str(e)}), 500
    finally: session.close()

# --- クリーンアップタスク ---
def cleanup_old_tasks():
    session = Session()
    try:
        day_ago = datetime.utcnow() - timedelta(hours=24)
        session.query(BackgroundTask).filter(BackgroundTask.created_at < day_ago).delete()
        week_ago = datetime.utcnow() - timedelta(days=7)
        session.query(ConversationHistory).filter(ConversationHistory.timestamp < week_ago).delete()
        month_ago = datetime.utcnow() - timedelta(days=90) # 仕様書に合わせて3ヶ月(90日)に変更
        session.query(HololiveNews).filter(HololiveNews.created_at < month_ago).delete()
        session.commit()
    except Exception as e:
        logger.error(f"クリーンアップエラー: {e}")
        session.rollback()
    finally:
        session.close()

def start_background_tasks():
    def periodic_cleanup():
        while True:
            time.sleep(3600)  # 1時間毎
            cleanup_old_tasks()
    threading.Thread(target=periodic_cleanup, daemon=True).start()
    logger.info("🚀 定期クリーンアップタスク開始")

# --- メイン実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001)) # 仕様書に合わせてポートを5001に変更
    host = '0.0.0.0'
    logger.info("=" * 70)
    logger.info("🚀 もちこAI フルスペック版 起動中...")
    logger.info(f"🌐 サーバーURL: {SERVER_URL}")
    logger.info("=" * 70)
    initialize_voice_directory()
    if VOICEVOX_ENABLED: check_voicevox_connection()
    start_background_tasks()
    schedule_hololive_news_updates()
    logger.info(f"🚀 Flask起動: {host}:{port}")
    logger.info(f"🗄️ データベース: {'✅' if DATABASE_URL else '❌'}")
    logger.info(f"🧠 Groq AI: {'✅' if groq_client else '❌'}")
    logger.info(f"🎤 VOICEVOX: {'✅' if VOICEVOX_ENABLED else '❌'}")
    logger.info(f"📰 ホロライブニュース: ✅ 1時間毎自動更新")
    logger.info(f"🔬 専門サイト検索: ✅ 有効 (Blender, CG, 脳科学, SL)")
    logger.info(f"⚡ 高速レスポンス & バックグラウンド処理: ✅ 有効")
    logger.info("=" * 70)
    app.run(host=host, port=port, debug=False, threaded=True)
