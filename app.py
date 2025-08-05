import os
import requests
import logging
import sys
import time
import threading
import json
import re
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus
from bs4 import BeautifulSoup

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

VOICEVOX_MAX_TEXT_LENGTH = 50
VOICEVOX_FAST_TIMEOUT = 10
CONVERSATION_HISTORY_TURNS = 2
VOICE_DIR = '/tmp/voices'

try:
    os.makedirs(VOICE_DIR, exist_ok=True)
    logger.info(f"📁 音声ディレクトリを確認/作成しました: {VOICE_DIR}")
except Exception as e:
    logger.error(f"❌ 音声ディレクトリ作成エラー: {e}")

# --- Secret/環境変数 読み込み ---
def get_secret(name):
    env_value = os.environ.get(name)
    if env_value:
        return env_value
    secret_file = f'/etc/secrets/{name}'
    try:
        with open(secret_file, 'r') as f:
            return f.read().strip()
    except Exception:
        return None

DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- クライアント初期化 ---
groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groqクライアント初期化成功")
    except Exception as e:
        logger.error(f"❌ Groqクライアント初期化エラー: {e}")

# --- VOICEVOX接続テスト ---
VOICEVOX_URLS = ['http://localhost:50021', 'http://127.0.0.1:50021', 'http://voicevox-engine:50021']
def find_working_voicevox_url(max_retries=3, retry_delay=2):
    urls_to_test = [url for url in ([VOICEVOX_URL_FROM_ENV] + VOICEVOX_URLS) if url]
    for url in urls_to_test:
        try:
            response = requests.get(f"{url}/version", timeout=5)
            if response.status_code == 200:
                logger.info(f"🎯 VOICEVOX URL決定: {url}")
                return url
        except requests.exceptions.RequestException:
            continue
    logger.warning("❌ 利用可能なVOICEVOX URLが見つかりません。音声機能は無効化されます。")
    return None

WORKING_VOICEVOX_URL = find_working_voicevox_url()
VOICEVOX_ENABLED = bool(WORKING_VOICEVOX_URL)

# --- 必須設定の確認と終了処理 ---
if not all([DATABASE_URL, groq_client]):
    logger.critical("FATAL: 必須設定(DATABASE_URL or GROQ_API_KEY)が不足しています。")
    sys.exit(1)

# --- Flask & DB 初期化 ---
app = Flask(__name__)
CORS(app)
engine = create_engine(DATABASE_URL)
Base = declarative_base()

class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
    user_name = Column(String(255), nullable=False)
    interaction_count = Column(Integer, default=0)

class ConversationHistory(Base):
    __tablename__ = 'conversation_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
# ★【最終版】Web検索 & 要約機能 (Wikipedia連携強化)                ★
# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def search_google_for_urls(query, num_results=3):
    try:
        search_url = f"https://www.google.com/search?q={quote_plus(query)}&hl=ja&lr=lang_ja"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        urls = [a['href'].split('/url?q=')[1].split('&sa=U')[0] for a in soup.select('a[href^="/url?q="]') if 'google.com' not in a['href']]
        return list(dict.fromkeys(urls))[:num_results]
    except Exception as e:
        logger.error(f"Google URL検索エラー: {e}")
        return []

def scrape_page_content(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        for tag in soup(['script', 'style', 'header', 'footer', 'nav', 'aside']):
            tag.decompose()
        return clean_text(soup.get_text())
    except Exception:
        return None

def summarize_with_llm(text, query):
    if not groq_client or not text: return "情報が見つからなかった..."
    prompt = f"""以下の記事を、ユーザーの質問「{query}」に答える形で、最も重要なポイントを箇条書きで3つに絞って簡潔に要約してください。

# 記事本文:
{text[:4000]}

# 要約:"""
    try:
        completion = groq_client.chat.completions.create(messages=[{"role": "system", "content": prompt}], model="llama3-8b-8192", temperature=0.2, max_tokens=500)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI要約エラー: {e}")
        return "ごめん、情報の要約中にエラーが起きちゃった..."

def deep_web_search(query):
    logger.info(f"🔍 Webディープサーチ開始: '{query}'")
    urls = search_google_for_urls(query)
    if not urls: return f"「{query}」についてWeb検索したけど、情報が見つからなかった...ごめんね！"
    for url in urls:
        content = scrape_page_content(url)
        if content and len(content) > 100:
            return summarize_with_llm(content, query)
    return "Webページを見たけど、うまく情報をまとめられなかった..."

def search_wikipedia_summary(query):
    try:
        clean_query = re.sub(r'(とは|について|教えて|知りたい)', '', query).strip()
        api_url = f"https://ja.wikipedia.org/api/rest_v1/page/summary/{quote_plus(clean_query)}"
        headers = {'User-Agent': 'MochikoAIAssistant/1.0'}
        logger.info(f"📚 Wikipedia検索: {clean_query}")
        response = requests.get(api_url, headers=headers, timeout=8)
        if response.status_code == 200:
            data = response.json()
            title, summary = data.get('title'), data.get('extract')
            if summary:
                return f"Wikipediaの「{title}」によると、{summary}"
        return None
    except Exception as e:
        logger.error(f"Wikipedia検索エラー: {e}")
        return None

# --- 質問タイプの判定 ---
def is_recommendation_request(message: str) -> bool:
    return any(keyword in message for keyword in ['おすすめ', 'オススメ', '人気', '流行'])

def is_knowledge_request(message: str) -> bool:
    if is_recommendation_request(message): return False
    return any(keyword in message for keyword in ['とは', 'について', '教えて', '知りたい'])

def extract_recommendation_topic(message: str) -> str | None:
    topics = {'映画': ['映画'], '音楽': ['音楽', '曲'], 'アニメ': ['アニメ'], '本': ['本', '書籍'], 'ゲーム': ['ゲーム']}
    for topic, keywords in topics.items():
        if any(kw in message for kw in keywords): return topic
    return None

# --- DB操作ヘルパー ---
def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user: user.interaction_count += 1
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
        session.add(user)
    session.commit()
    return {'uuid': user.user_uuid, 'name': user.user_name, 'count': user.interaction_count}

def get_conversation_history(session, uuid, turns=CONVERSATION_HISTORY_TURNS):
    history = session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(turns * 2).all()
    return reversed(history)

# --- AI応答生成のコアロジック ---
def generate_ai_response(user_data, message, history):
    if not groq_client: return "あてぃし、今ちょっと調子悪いかも...またあとで話そ！"

    search_info = ""
    if is_recommendation_request(message):
        topic = extract_recommendation_topic(message)
        query = f"最新 {topic} 人気ランキング" if topic else "最近 話題のもの ランキング"
        logger.info(f"📈 おすすめ検索を実行: '{query}'")
        search_info = deep_web_search(query)
    elif is_knowledge_request(message):
        logger.info(f"📚 Wikipedia＆Web検索を実行: '{message}'")
        wiki_summary = search_wikipedia_summary(message)
        google_summary = deep_web_search(message)
        search_info = f"""
【Wikipediaの要約】
{wiki_summary or "見つかりませんでした。"}

【関連する最新Web情報の要約】
{google_summary or "見つかりませんでした。"}
"""
    system_prompt = f"""あなたは「もちこ」という名前の、賢くて親しみやすいギャルAIです。ユーザー「{user_data['name']}」さんと会話しています。

# もちこのルール:
- 自分のことは「あてぃし」と呼びます。
- 明るくフレンドリーなギャル口調（例：「まじ？」「～って感じ」「うける」「～ぢゃん？」）で、簡潔に話します。
- 以下の【Web検索の要約結果】を元に、自分の言葉で分かりやすく説明してください。要約の丸写しは絶対ダメ。
- ★最重要★ ユーザーが「～とは」「～について」と質問した時は、【Wikipediaの要約】と【関連する最新Web情報の要約】の2つの情報が提供されます。その場合、必ず以下の構成で答えてください:
  1. まず、Wikipediaの情報を使って「〇〇っていうのは、要は～ってことなんだよね！」みたいに、基本的な意味を説明する。
  2. 次に、「ちなみに、最近のWebニュースだと～って話もあるみたいだよ！」みたいに、【関連する最新Web情報の要約】の内容を付け加えて、補足情報や最新の動向を話す。
- 「おすすめ」を聞かれた時は、ランキング情報から面白そうなものをピックアップして提案してね。
- 過去の会話を読んで、文脈に合った返事をしてください。

# 【Web検索の要約結果】:
{search_info or 'なし'}
"""

    messages = [{"role": "system", "content": system_prompt}]
    for past_msg in history:
        messages.append({"role": past_msg.role, "content": past_msg.content})
    messages.append({"role": "user", "content": message})

    try:
        completion = groq_client.chat.completions.create(messages=messages, model="llama3-8b-8192", temperature=0.75, max_tokens=250)
        response = completion.choices[0].message.content.strip()
        logger.info(f"✅ AI応答生成成功: {response}")
        return response
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "ごめん、今ちょっと考えがまとまんない！また後で話しかけて！"

# --- 音声関連 ---
voice_cache = {}
CACHE_MAX_SIZE = 100
cache_lock = threading.Lock()
voice_files = {}
voice_files_lock = threading.Lock()

def generate_voice_fast(text, speaker_id=3):
    if not VOICEVOX_ENABLED or not text: return None
    text = text[:VOICEVOX_MAX_TEXT_LENGTH]
    cache_key = f"{hash(text)}_{speaker_id}"
    with cache_lock:
        if cached := voice_cache.get(cache_key): return cached
    try:
        query_res = requests.post(f"{WORKING_VOICEVOX_URL}/audio_query", params={'text': text, 'speaker': speaker_id}, timeout=VOICEVOX_FAST_TIMEOUT)
        query_res.raise_for_status()
        synth_res = requests.post(f"{WORKING_VOICEVOX_URL}/synthesis", params={'speaker': speaker_id}, json=query_res.json(), timeout=VOICEVOX_FAST_TIMEOUT * 6)
        synth_res.raise_for_status()
        voice_data = synth_res.content
        with cache_lock:
            if len(voice_cache) >= CACHE_MAX_SIZE: voice_cache.pop(next(iter(voice_cache)))
            voice_cache[cache_key] = voice_data
        return voice_data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 音声合成リクエストエラー: {e}")
        return None

def background_voice_generation(text, filename, speaker_id=3):
    voice_data = generate_voice_fast(text, speaker_id)
    if voice_data:
        try:
            filepath = os.path.join(VOICE_DIR, filename)
            with open(filepath, 'wb') as f: f.write(voice_data)
            with voice_files_lock: voice_files[filename] = {'data': voice_data}
            logger.info(f"✅ 音声ファイル保存成功: {filename}")
        except Exception as e:
            logger.error(f"❌ 音声ファイル保存エラー: {e}")

# --- Flask ルート定義 ---
@app.route('/')
def index():
    return jsonify({'service': 'もちこ AI Assistant (Full-Function Ver.)', 'status': 'running'})

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'voicevox': 'enabled' if VOICEVOX_ENABLED else 'disabled'})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json or {}
        user_uuid, user_name, message = data.get('uuid'), data.get('name'), data.get('message', '')
        if not (user_uuid and user_name): return "Error: uuid and name required", 400

        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message}'")
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        ai_text = generate_ai_response(user_data, message, list(history))

        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()

        audio_url = ""
        if VOICEVOX_ENABLED:
            filename = f"voice_{user_uuid[:8]}_{int(time.time() * 1000)}.wav"
            audio_url = f'/voice/{filename}'
            threading.Thread(target=background_voice_generation, args=(ai_text, filename)).start()

        return app.response_class(response=f"{ai_text}|{audio_url}", status=200, mimetype='text/plain; charset=utf-8')
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントエラー: {e}", exc_info=True)
        session.rollback()
        return "Error: Internal server error", 500
    finally:
        session.close()

@app.route('/voice/<filename>')
def serve_voice(filename):
    with voice_files_lock:
        if file_data := voice_files.get(filename):
            return app.response_class(response=file_data['data'], mimetype='audio/wav')
    filepath = os.path.join(VOICE_DIR, filename)
    if os.path.exists(filepath):
        return send_from_directory(VOICE_DIR, filename, mimetype='audio/wav')
    return "Voice not found or still generating", 404

# --- メイン実行部分 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0'
    logger.info(f"🚀 Flask アプリケーションを開始します (Full-Function Ver.): {host}:{port}")
    app.run(host=host, port=port, debug=False)
