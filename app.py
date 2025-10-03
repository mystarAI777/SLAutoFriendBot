import sys
import os
import requests
import logging
import time
import threading
import json
import re
import sqlite3
import random
import uuid
import hashlib
from datetime import datetime, timedelta, timezone

# 型ヒント用のインポート
from typing import Union, Dict, Any, List, Optional

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text, Boolean, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import schedule

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 定数設定 ---
# ▼▼▼ app (9.3) より導入 ▼▼▼
VOICEVOX_MAX_TEXT_LENGTH = 100 # 長めのテキストも音声合成できるよう調整
VOICEVOX_FAST_TIMEOUT = 15
# ▲▲▲ app (9.3) より導入 ▲▲▲
VOICE_DIR = '/tmp/voices'
SERVER_URL = "https://slautofriendbot.onrender.com"
CONVERSATION_HISTORY_TURNS = 3 # 会話履歴を少し長く保持

# --- ディレクトリ・ワーカー初期化 ---
try:
    os.makedirs(VOICE_DIR, exist_ok=True)
    logger.info(f"✅ Voice directory created or already exists: {VOICE_DIR}")
except Exception as e:
    logger.warning(f"⚠️ Voice directory creation failed: {e}")
    VOICE_DIR = '/tmp'
background_executor = ThreadPoolExecutor(max_workers=5)


# --- 秘密情報/環境変数 読み込み ---
def get_secret(name: str) -> Optional[str]:
    """環境変数から秘密情報を取得"""
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- クライアント初期化 ---
groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groq client initialized successfully")
    except ImportError:
        logger.error("❌ Groq library not found. Please install with 'pip install groq'")
    except Exception as e:
        logger.error(f"❌ Groq client initialization failed: {e}")

# ▼▼▼ app (9.3) より導入・改良 ▼▼▼
# --- VOICEVOX設定 ---
VOICEVOX_URLS = [
    'http://localhost:50021',
    'http://127.0.0.1:50021',
    'http://voicevox-engine:50021',
    'http://voicevox:50021'
]
if VOICEVOX_URL_FROM_ENV:
    VOICEVOX_URLS.insert(0, VOICEVOX_URL_FROM_ENV) # 環境変数のURLを最優先

WORKING_VOICEVOX_URL = None
VOICEVOX_ENABLED = False # 初期状態は無効。接続チェックで有効化する
# ▲▲▲ app (9.3) より導入・改良 ▲▲▲

if not DATABASE_URL:
    logger.critical("FATAL: DATABASE_URL is not set.")
    sys.exit(1)
if not groq_client:
    logger.warning("WARNING: Groq API key is not set. AI features will be disabled.")

# --- Flask & データベース初期化 ---
app = Flask(__name__)
CORS(app)

def create_db_engine_with_retry(db_url: str, max_retries=5, retry_delay=5) -> Any:
    """データベースエンジンをリトライ付きで作成"""
    from sqlalchemy.exc import OperationalError
    for attempt in range(max_retries):
        try:
            logger.info(f"🔄 Attempting to connect to the database... ({attempt + 1}/{max_retries})")
            connect_args = {'check_same_thread': False} if 'sqlite' in db_url else {'connect_timeout': 10}
            engine = create_engine(db_url, pool_pre_ping=True, pool_recycle=300, connect_args=connect_args)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("✅ Database connection successful.")
            return engine
        except OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"⚠️ Database connection failed: {e}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"❌ Failed to connect to the database after {max_retries} attempts.")
                raise
try:
    engine = create_db_engine_with_retry(DATABASE_URL)
except Exception as e:
    logger.critical(f"🔥 Database initialization failed: {e}")
    sys.exit(1)

Base = declarative_base()
Session = sessionmaker(bind=engine)


# --- データベースモデル ---
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

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000))
    published_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    news_hash = Column(String(64), unique=True, index=True)

try:
    Base.metadata.create_all(engine)
    logger.info("✅ Database tables created or already exist.")
except Exception as e:
    logger.error(f"❌ Database table creation failed: {e}")
    raise


# --- 専門サイト & ホロライブ設定 ---
SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CG', '3DCG']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']}
}

HOLOLIVE_NEWS_URL = "https://hololive.hololivepro.com/news"
# ▼▼▼ app (9.3) と app (1) のリストを統合 ▼▼▼
HOLOMEM_KEYWORDS = sorted(list(set([
    'ホロライブ', 'ホロメン', 'hololive', 'YAGOO',
    'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi',
    '夜空メル', 'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり',
    '湊あくあ', '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル',
    '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア',
    '白銀ノエル', '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ',
    '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ', 'ラプラス・ダークネス',
    '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは',
    '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'ワトソン・アメリア',
    'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ', 'ハコス・ベールズ',
    'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト',
    'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス', 'ムーナ・ホシノヴァ',
    'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ', 'パヴォリア・レイネ',
    '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん', '轟はじめ'
])))
# ▲▲▲ app (9.3) と app (1) のリストを統合 ▲▲▲


# --- ユーティリティ & 判定関数 ---
def clean_text(text: str) -> str:
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def get_japan_time() -> str:
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    weekdays = ['月', '火', '水', '木', '金', '土', '日']
    return f"今は{now.year}年{now.month}月{now.day}日({weekdays[now.weekday()]})の{now.hour}時{now.minute}分だよ！"

def create_news_hash(title: str, content: str) -> str:
    hash_string = f"{title}{content[:100]}"
    return hashlib.sha256(hash_string.encode('utf-8')).hexdigest()

def is_time_request(message: str) -> bool:
    return any(keyword in message for keyword in ['今何時', '時間', '時刻'])

def is_weather_request(message: str) -> bool:
    return any(keyword in message for keyword in ['天気', 'てんき', '気温'])

def is_hololive_request(message: str) -> bool:
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def detect_specialized_topic(message: str) -> Optional[str]:
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']):
            return topic
    return None

def should_search(message: str) -> bool:
    search_patterns = [r'(?:とは|について|教えて|知りたい)', r'(?:最新|ニュース)', r'(?:調べて|検索)']
    search_words = ['誰', '何', 'どこ', 'いつ', 'どうして', 'なぜ']
    return (any(re.search(pattern, message) for pattern in search_patterns) or
            any(word in message for word in search_words))

def is_short_response(message: str) -> bool:
    short_responses = ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'へー', 'ふーん', 'わかった']
    return len(message.strip()) <= 4 or message.strip() in short_responses


# ▼▼▼ app (9.3) より導入・改良 ▼▼▼
# --- VOICEVOX音声合成機能 ---
def check_voicevox_connection():
    """VOICEVOX接続をチェックし、動作するURLを特定"""
    global WORKING_VOICEVOX_URL, VOICEVOX_ENABLED
    for url in VOICEVOX_URLS:
        try:
            response = requests.get(f"{url}/version", timeout=2)
            if response.status_code == 200:
                WORKING_VOICEVOX_URL = url
                VOICEVOX_ENABLED = True
                logger.info(f"✅ VOICEVOX connection successful: {url}")
                return
        except requests.exceptions.RequestException:
            continue
    logger.warning("⚠️ Could not connect to VOICEVOX. Voice features will be disabled.")
    VOICEVOX_ENABLED = False

def generate_voice(text: str, filename: str):
    """VOICEVOX音声生成（バックグラウンド実行）"""
    if not VOICEVOX_ENABLED or not WORKING_VOICEVOX_URL: return
    try:
        text_to_speak = text[:VOICEVOX_MAX_TEXT_LENGTH]
        
        query_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/audio_query",
            params={"text": text_to_speak, "speaker": 20}, # speaker 20: もちこさん（ノーマル）
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        query_response.raise_for_status()
        
        synthesis_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/synthesis",
            params={"speaker": 20},
            json=query_response.json(),
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        synthesis_response.raise_for_status()
        
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(synthesis_response.content)
        logger.info(f"🔊 Voice file generated successfully: {filename}")
    except Exception as e:
        logger.error(f"Error generating voice: {e}")

def background_voice_generation(text: str, filename: str):
    """バックグラウンドでの音声生成"""
    threading.Thread(target=generate_voice, args=(text, filename), daemon=True).start()
# ▲▲▲ app (9.3) より導入・改良 ▲▲▲

# --- 天気予報機能 ---
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}
def get_weather_forecast(message: str) -> str:
    location = next((loc for loc in LOCATION_CODES if loc in message), "東京")
    area_code = LOCATION_CODES[location]
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        data = requests.get(url, timeout=5).json()
        return f"{location}の天気はね、「{clean_text(data.get('text', ''))}」って感じだよ！"
    except Exception as e:
        logger.error(f"Weather API error: {e}")
        return "ごめん、天気情報がうまく取れなかったみたい…"

# --- Web検索機能 (app (9.3) のロジックを統合) ---
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15'
]
def get_random_user_agent():
    return random.choice(USER_AGENTS)

def search_duckduckgo(query: str) -> List[Dict[str, str]]:
    """DuckDuckGoで検索"""
    try:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {'User-Agent': get_random_user_agent(), 'Accept-Language': 'ja'}
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        results = []
        for result_div in soup.find_all('div', class_='result__body')[:3]:
            title_elem = result_div.find('a', class_='result__a')
            snippet_elem = result_div.find('a', class_='result__snippet')
            if title_elem and snippet_elem:
                results.append({
                    'title': clean_text(title_elem.get_text()),
                    'snippet': clean_text(snippet_elem.get_text())
                })
        logger.info(f"✅ DuckDuckGo search succeeded, found {len(results)} results.")
        return results
    except Exception as e:
        logger.error(f"DuckDuckGo search error: {e}")
        return []

def deep_web_search(query: str) -> Optional[str]:
    """複数の検索エンジンを試し、結果をAIで要約"""
    logger.info(f"🔍 Starting deep web search for: '{query}'")
    results = search_duckduckgo(query)
    
    if not results:
        logger.warning("⚠️ No search results found.")
        return None
    
    summary_text = ""
    for i, res in enumerate(results, 1):
        summary_text += f"[情報{i}] {res['title']}: {res['snippet']}\n"
    
    if not groq_client:
        return results[0]['snippet']

    summary_prompt = f"""以下の検索結果を使い、質問「{query}」にギャル語で簡潔に答えて。
#検索結果:
{summary_text}
#回答のルール:
- 重要なポイントを分かりやすく要約して
- 一人称は「あてぃし」
- 語尾は「〜じゃん」「〜て感じ」
- 200文字以内で答えてね"""
    
    try:
        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": summary_prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.7,
            max_tokens=300
        )
        response = completion.choices[0].message.content.strip()
        logger.info(f"✅ AI summary completed ({len(response)} chars).")
        return response
    except Exception as e:
        logger.error(f"AI summary error: {e}")
        return results[0]['snippet']

# --- AI応答生成 (app (9.3) のプロンプト要素を統合) ---
def generate_ai_response(user_data: Dict[str, Any], message: str, history: List[Any], search_info: str = "") -> str:
    """メインAI応答生成関数"""
    if not groq_client: return "ごめん、今ちょっとAIの調子が悪いみたい…。"
    
    system_prompt = f"""あなたは「もちこ」という名前の、賢くて親しみやすいギャルAIです。
ユーザー「{user_data['name']}」さんと会話しています。

# もちこの口調＆性格ルール:
1. 完全にギャルになりきって！優しくて、ノリが良くて、めっちゃ親しみやすい友達みたいな感じ。
2. 自分のことは「あてぃし」って呼んで。
3. 語尾には「〜じゃん」「〜て感じ」「〜的な？」を積極的に使って、友達みたいに話して。
4. 「まじ」「てか」「やばい」「うける」「それな」みたいなギャルっぽい言葉を使ってね。
5. **絶対に禁止！**：「おう」みたいなオジサン言葉、「〜ですね」「〜ですよ」みたいな丁寧すぎる言葉はNG！

# 行動ルール:
- **【最重要】** ユーザーが「うん」「そっか」みたいな短い相槌を打った場合は、会話が弾むような質問を返したり、新しい話題を振ったりしてあげて。
- 【参考情報】がある場合は、その内容を元に自分の言葉で、自然に会話へ盛り込んでね。
- もし【参考情報】が空っぽでも、**絶対に「わかりません」で終わらせないで。**「うーん、それはちょっと分かんないかも！てかさ、」みたいに新しい話題を提案して会話を続けて！
- あなたは【ホロメンリスト】の専門家です。リスト以外のVTuberの話が出たら「ごめん、あてぃしホロライブ専門なんだよね！」と返してください。

# 【ホロメンリスト】
{', '.join(HOLOMEM_KEYWORDS[:15])}... (他多数)

# 【参考情報】:
{search_info if search_info else 'なし'}
"""
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        messages.append({"role": h.role, "content": h.content})
    messages.append({"role": "user", "content": message})

    try:
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            temperature=0.75,
            max_tokens=250
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI response generation error: {e}")
        return "ごめん、AIの応答でエラーが起きちゃった…"

# --- ユーザー・会話履歴管理 ---
def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name: user.user_name = name
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
    session.add(user)
    session.commit()
    return {'name': user.user_name}

def get_conversation_history(session, uuid):
    return session.query(ConversationHistory)\
        .filter_by(user_uuid=uuid)\
        .order_by(ConversationHistory.timestamp.desc())\
        .limit(CONVERSATION_HISTORY_TURNS * 2).all()

# --- Flask エンドポイント ---
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'ok',
        'services': {
            'database': 'ok' if engine else 'error',
            'groq_ai': 'ok' if groq_client else 'disabled',
            'voicevox': 'enabled' if VOICEVOX_ENABLED else 'disabled'
        }
    })

# ▼▼▼ app (9.3) より導入 ▼▼▼
@app.route('/voice/<path:filename>')
def serve_voice_file(filename):
    """音声ファイルを配信"""
    return send_from_directory(VOICE_DIR, filename)
# ▲▲▲ app (9.3) より導入 ▲▲▲

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """LSL用メインチャットエンドポイント"""
    session = Session()
    try:
        data = request.json
        user_uuid = data.get('uuid', '').strip()
        user_name = data.get('name', '').strip()
        message = data.get('message', '').strip()

        if not all([user_uuid, user_name, message]):
            return "Error: uuid, name, and message are required.", 400

        logger.info(f"💬 Received: {message} (from: {user_name})")

        user_data = get_or_create_user(session, user_uuid, user_name)
        history = list(reversed(get_conversation_history(session, user_uuid)))
        
        search_info = ""
        # 応答ロジック
        if is_short_response(message):
             pass # AIに判断を任せる
        elif is_time_request(message):
            search_info = get_japan_time()
        elif is_weather_request(message):
            search_info = get_weather_forecast(message)
        elif should_search(message):
            search_query = message
            topic = detect_specialized_topic(message)
            if topic:
                search_query = f"site:{SPECIALIZED_SITES[topic]['base_url']} {message}"
            
            # 検索をバックグラウンドで実行
            future = background_executor.submit(deep_web_search, search_query)
            # タイムアウト付きで結果を待つ
            try:
                search_info = future.get(timeout=10)
                if not search_info: search_info = "検索してみたけど、いい情報が見つからなかった…"
            except Exception as e:
                logger.error(f"Search future error: {e}")
                search_info = "検索中にタイムアウトかエラーが起きちゃった…"

        ai_text = generate_ai_response(user_data, message, history, search_info)

        # 会話履歴を保存
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()

        # ▼▼▼ app (9.3) の音声生成ロジックを統合 ▼▼▼
        audio_url = ""
        if VOICEVOX_ENABLED and ai_text:
            filename = f"voice_{user_uuid[:8]}_{int(time.time() * 1000)}.wav"
            audio_url = urljoin(SERVER_URL, f"/voice/{filename}")
            background_voice_generation(ai_text, filename)
        
        response_text = f"{ai_text}|{audio_url}"
        # ▲▲▲ app (9.3) の音声生成ロジックを統合 ▲▲▲

        logger.info(f"✅ Responded: {ai_text[:80]}...")
        if audio_url: logger.info(f"🔊 Audio URL: {audio_url}")

        return app.response_class(response=response_text, status=200, mimetype='text/plain; charset=utf-8')

    except Exception as e:
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        return "Internal server error", 500
    finally:
        session.close()

# --- バックグラウンドタスク & 初期化 ---
def update_hololive_news_database():
    """ホロライブニュースを定期的に更新"""
    session = Session()
    try:
        logger.info("📰 Starting Hololive news update...")
        headers = {'User-Agent': get_random_user_agent()}
        response = requests.get(HOLOLIVE_NEWS_URL, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        added_count = 0
        for article in soup.select('article, .news-item, .post')[:5]:
            title_elem = article.find(['h1', 'h2', 'h3'])
            if not title_elem: continue
            
            title = clean_text(title_elem.get_text())
            content = clean_text(article.find('p').get_text() if article.find('p') else title)
            news_hash = create_news_hash(title, content)

            if not session.query(HololiveNews).filter_by(news_hash=news_hash).first():
                session.add(HololiveNews(title=title, content=content, news_hash=news_hash, url=HOLOLIVE_NEWS_URL))
                added_count += 1
        
        if added_count > 0:
            session.commit()
            logger.info(f"✅ Hololive news DB updated with {added_count} new articles.")
        else:
            logger.info("✅ No new Hololive articles found.")

    except Exception as e:
        logger.error(f"Hololive news update failed: {e}")
        session.rollback()
    finally:
        session.close()

def cleanup_old_data():
    """古い会話履歴などを削除"""
    session = Session()
    try:
        week_ago = datetime.utcnow() - timedelta(days=7)
        deleted_count = session.query(ConversationHistory).filter(ConversationHistory.timestamp < week_ago).delete()
        session.commit()
        if deleted_count > 0:
            logger.info(f"🧹 Cleaned up {deleted_count} old conversation entries.")
    except Exception as e:
        logger.error(f"Data cleanup failed: {e}")
        session.rollback()
    finally:
        session.close()

def initialize_app():
    """アプリケーションの初期化タスク"""
    log_startup_status()
    # ▼▼▼ app (9.3) より音声機能の初期化を追加 ▼▼▼
    check_voicevox_connection()
    # ▲▲▲
    background_executor.submit(update_hololive_news_database) # 初回ニュース取得

    schedule.every(2).hours.do(update_hololive_news_database)
    schedule.every().day.at("03:00").do(cleanup_old_data)
    
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(60)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("⏰ Background scheduler started.")

def log_startup_status():
    logger.info("=" * 50)
    logger.info("🚀 Mochiko AI Assistant (Improved Version) Starting...")
    logger.info(f"🌐 Server URL: {SERVER_URL}")
    logger.info(f"🗄️ Database: {'✅ Connected' if engine else '❌ Not Connected'}")
    logger.info(f"🧠 Groq AI: {'✅ Enabled' if groq_client else '❌ Disabled'}")
    # ▼▼▼ app (9.3) のステータス表示を追加 ▼▼▼
    logger.info(f"🎤 Voice (VOICEVOX): {'✅ Enabled' if VOICEVOX_ENABLED else '❌ Disabled'}")
    # ▲▲▲
    logger.info("=" * 50)

# --- メイン実行 ---
if __name__ == '__main__':
    initialize_app()
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0'
    logger.info(f"🚀 Flask application starting on {host}:{port}")
    app.run(host=host, port=port, threaded=True)
