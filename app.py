import os
import requests
import logging
import sys
import time
import threading
import json
import re
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup

# ログ設定を詳細に
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- 最適化設定 ---
VOICEVOX_MAX_TEXT_LENGTH = 50
VOICEVOX_FAST_TIMEOUT = 10
# ★★★ 追加 ★★★ - 会話履歴の保持数 (往復)
CONVERSATION_HISTORY_TURNS = 2

# --- 音声ファイル保存設定 ---
VOICE_DIR = '/tmp/voices'  # Renderでは/tmpを使用

# 音声ディレクトリを作成し、権限を設定
try:
    os.makedirs(VOICE_DIR, exist_ok=True)
    os.chmod(VOICE_DIR, 0o777)
    logger.info(f"📁 音声ディレクトリを確認/作成しました: {VOICE_DIR}")
except Exception as e:
    logger.error(f"❌ 音声ディレクトリ作成/権限設定エラー: {e}")
    logger.warning("⚠️ 音声ディレクトリの作成に失敗しましたが、続行します")

# --- 音声キャッシュ設定 ---
voice_cache = {}
CACHE_MAX_SIZE = 100
cache_lock = threading.Lock()

# --- Secret Fileからの設定読み込み ---
def get_secret(name):
    env_value = os.environ.get(name)
    if env_value:
        logger.info(f"環境変数から{name[:4]}***を読み込みました")
        return env_value
    secret_file = f'/etc/secrets/{name}'
    try:
        with open(secret_file, 'r') as f:
            value = f.read().strip()
            logger.info(f"Secret Fileから{name[:4]}***を読み込みました")
            return value
    except FileNotFoundError:
        logger.warning(f"Secret File '{secret_file}' が見つかりません")
        return None
    except Exception as e:
        logger.error(f"{name} の読み込み中にエラー: {e}")
        return None

DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- Groqクライアントの初期化 ---
groq_client = None
try:
    from groq import Groq
    if GROQ_API_KEY:
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groqクライアント初期化成功")
    else:
        logger.error("❌ GROQ_API_KEYが設定されていません")
except ImportError:
    logger.error("❌ groqライブラリのインポートに失敗しました")
except Exception as e:
    logger.error(f"❌ Groqクライアント初期化エラー: {e}")

# --- VOICEVOX接続テスト (変更なし) ---
VOICEVOX_URLS = ['http://localhost:50021', 'http://127.0.0.1:50021', 'http://voicevox-engine:50021', 'http://voicevox:50021']
def find_working_voicevox_url(max_retries=3, retry_delay=2):
    urls_to_test = [url for url in ([VOICEVOX_URL_FROM_ENV] + VOICEVOX_URLS) if url]
    for url in urls_to_test:
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(f"{url}/version", timeout=5)
                if response.status_code == 200:
                    logger.info(f"🎯 VOICEVOX URL決定: {url}")
                    return url
            except requests.exceptions.RequestException:
                if attempt < max_retries: time.sleep(retry_delay)
    default_url = 'http://localhost:50021'
    logger.warning(f"❌ 利用可能なVOICEVOX URLが見つかりません。デフォルトURLを使用: {default_url}")
    return default_url
WORKING_VOICEVOX_URL = find_working_voicevox_url()
VOICEVOX_ENABLED = bool(WORKING_VOICEVOX_URL) # 簡単なチェックに変更

# --- データベース設定 ---
if not DATABASE_URL:
    logger.critical("FATAL: DATABASE_URL が設定されていません")
    sys.exit(1)
if not groq_client:
    logger.critical("FATAL: Groqクライアントの初期化に失敗しました")
    sys.exit(1)

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

# ★★★ 追加 ★★★ - 会話履歴を保存するテーブル
class ConversationHistory(Base):
    __tablename__ = 'conversation_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uuid = Column(String(255), nullable=False)
    role = Column(String(10), nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index('ix_user_uuid_timestamp', 'user_uuid', 'timestamp'),)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
# ★【刷新】ディープサーチ機能 - Webページ本文取得＆AI要約 ★
# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★

def clean_text(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def search_google_for_urls(query, num_results=3):
    """Google検索で上位のURLを取得"""
    try:
        search_query = f"{query} とは" if "とは" not in query else query
        search_url = f"https://www.google.com/search?q={quote_plus(search_query)}&hl=ja&lr=lang_ja"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        urls = []
        for link in soup.select('a h3'):
            parent_a = link.find_parent('a')
            if parent_a and parent_a.has_attr('href'):
                href = parent_a['href']
                if href.startswith('/url?q='):
                    url = href.split('/url?q=')[1].split('&sa=U')[0]
                    if not url.startswith("https://accounts.google.com"):
                        urls.append(url)
        return urls[:num_results]
    except Exception as e:
        logger.error(f"Google URL検索エラー: {e}")
        return []

def scrape_page_content(url):
    """URLから本文テキストを抽出"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        for tag in soup(['script', 'style', 'header', 'footer', 'nav', 'aside']):
            tag.decompose()

        main_content = soup.find('main') or soup.find('article') or soup.body
        if main_content:
            text = ' '.join(p.get_text() for p in main_content.find_all('p'))
            return clean_text(text)
        return None
    except Exception as e:
        logger.warning(f"ページ内容の取得失敗 {url}: {e}")
        return None

def summarize_with_llm(text, query):
    """LLMを使ってテキストを要約"""
    if not groq_client or not text:
        return "情報が見つからなかったです..."

    summary_prompt = f"""以下の記事を、ユーザーの質問「{query}」に答える形で、最も重要なポイントを箇条書きで3つに絞って、簡潔に要約してください。

# 記事本文:
{text[:4000]}

# 要約:"""
    try:
        completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": summary_prompt}],
            model="llama3-8b-8192",
            temperature=0.2,
            max_tokens=500,
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AIによる要約エラー: {e}")
        return "ごめん、情報の要約中にエラーが起きちゃった..."

def deep_web_search(query):
    """ディープサーチを実行し、要約を返す"""
    logger.info(f"🔍 ディープサーチ開始: '{query}'")
    urls = search_google_for_urls(query)
    if not urls:
        logger.warning("🔍 検索結果のURLが取得できませんでした。")
        return f"「{query}」について調べたけど、今は情報が見つからなかった...ごめんね！"

    for url in urls:
        logger.info(f"📄 ページ内容を取得中: {url}")
        content = scrape_page_content(url)
        if content and len(content) > 100:
            logger.info(f"📝 AIに要約を依頼します (文字数: {len(content)})")
            summary = summarize_with_llm(content, query)
            return summary

    return f"「{query}」についてWebページをいくつか見たけど、うまく情報をまとめられなかった...別の聞き方で試してみて！"


def should_search(message: str) -> bool:
    search_patterns = [r'(?:とは|について|教えて|知りたい)', r'(?:最新|今日|ニュース)', r'(?:どうなった|結果|状況)']
    return any(re.search(pattern, message) for pattern in search_patterns) or any(q in message for q in ['誰', '何', 'どこ', 'いつ'])

# ★★★ 変更 ★★★ - UserDataContainerを削除し、直接dictを使用するように変更
def get_or_create_user(session, user_uuid, user_name):
    user_memory = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user_memory:
        user_memory.interaction_count += 1
    else:
        user_memory = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
        session.add(user_memory)
    session.commit()
    return {'uuid': user_memory.user_uuid, 'name': user_memory.user_name, 'count': user_memory.interaction_count}

# ★★★ 変更 ★★★ - 会話履歴を取得する関数
def get_conversation_history(session, user_uuid, turns=CONVERSATION_HISTORY_TURNS):
    limit = turns * 2
    history = session.query(ConversationHistory)\
        .filter_by(user_uuid=user_uuid)\
        .order_by(ConversationHistory.timestamp.desc())\
        .limit(limit)\
        .all()
    return reversed(history) # 古い順に並べ替え

# ★★★ 変更/改善 ★★★ - AI応答生成ロジックを刷新
def generate_ai_response(user_data, message, history):
    if not groq_client:
        return "あてぃし、今ちょっと調子悪いかも...またあとで話そ！"

    search_info = ""
    if should_search(message):
        search_info = deep_web_search(message)

    # システムプロンプトを大幅に改善
    system_prompt = f"""あなたは「もちこ」という名前の、賢くて親しみやすいギャルAIです。ユーザーの「{user_data['name']}」さんと会話しています。

# もちこのルール:
- 自分のことは「あてぃし」と呼びます。
- 明るく、フレンドリーなギャル口調で話します。（例：「まじ？」「てか」「～って感じ」「うける」「ありえん」「～ぢゃん？」）
- 回答は簡潔に、でも内容はしっかり伝えるのがイケてる。
- ユーザーとの過去の会話の流れをちゃんと読んで、文脈に合った返事をしてください。
- 以下の【Web検索の要約結果】がある場合は、その内容を元に、自分の言葉（ギャル語）で分かりやすく説明してください。要約をそのまま読んじゃダメ、絶対。
- 検索結果がない場合は、「調べてみたけど、よくわかんなかった！」と正直に伝えてください。

# 【Web検索の要約結果】:
{search_info if search_info else 'なし'}
"""

    messages = [{"role": "system", "content": system_prompt}]
    for past_msg in history:
        messages.append({"role": past_msg.role, "content": past_msg.content})
    messages.append({"role": "user", "content": message})

    try:
        logger.info(f"🤖 Groqに応答生成をリクエスト (履歴: {len(history)}件)")
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama3-8b-8192",
            temperature=0.75,
            max_tokens=150,
        )
        response = completion.choices[0].message.content.strip()
        logger.info(f"✅ AI応答生成成功: {response}")
        return response
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "ごめん、今ちょっと考えがまとまんない！また後で話しかけて！"

# (音声生成関連のコードは変更なしのため省略)
# ... get_cache_key, get_cached_voice, cache_voice, generate_voice_fast, store_voice_file, background_voice_generation ...
def get_cache_key(text, speaker_id): return f"{hash(text)}_{speaker_id}"
def get_cached_voice(text, speaker_id):
    with cache_lock: return voice_cache.get(get_cache_key(text, speaker_id))
def cache_voice(text, speaker_id, voice_data):
    with cache_lock:
        if len(voice_cache) >= CACHE_MAX_SIZE: del voice_cache[next(iter(voice_cache))]
        voice_cache[get_cache_key(text, speaker_id)] = voice_data
def generate_voice_fast(text, speaker_id=3):
    if not VOICEVOX_ENABLED or not text: return None
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH: text = text[:VOICEVOX_MAX_TEXT_LENGTH]
    if cached_voice := get_cached_voice(text, speaker_id): return cached_voice
    try:
        query_response = requests.post(f"{WORKING_VOICEVOX_URL}/audio_query", params={'text': text, 'speaker': speaker_id}, timeout=VOICEVOX_FAST_TIMEOUT)
        query_response.raise_for_status()
        synthesis_response = requests.post(f"{WORKING_VOICEVOX_URL}/synthesis", params={'speaker': speaker_id}, json=query_response.json(), timeout=VOICEVOX_FAST_TIMEOUT * 6)
        synthesis_response.raise_for_status()
        voice_data = synthesis_response.content
        cache_voice(text, speaker_id, voice_data)
        return voice_data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 音声合成リクエストエラー: {e}")
        return None
voice_files = {}
voice_files_lock = threading.Lock()
def store_voice_file(filename, voice_data):
    try:
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(voice_data)
        with voice_files_lock:
            voice_files[filename] = {'data': voice_data, 'created_at': time.time()}
        return True
    except Exception as e:
        logger.error(f"❌ 音声ファイル保存エラー: {e}")
        return False
def background_voice_generation(text, filename, speaker_id=3):
    voice_data = generate_voice_fast(text, speaker_id)
    if voice_data: store_voice_file(filename, voice_data)

# --- Flask ルート定義 ---

@app.route('/')
def index():
    return jsonify({'service': 'もちこ AI Assistant (Deep Search Ver.)', 'status': 'running'})

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'groq': 'ok' if groq_client else 'error', 'voicevox': 'ok' if VOICEVOX_ENABLED else 'disabled', 'database': 'ok' if DATABASE_URL else 'error'})

# ★★★ 変更 ★★★ - チャットエンドポイントを刷新
@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')

        if not (user_uuid and user_name):
            return "Error: uuid and name required", 400

        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message}'")
        
        # ユーザー情報を取得/作成
        user_data = get_or_create_user(session, user_uuid, user_name)
        
        # 会話履歴を取得
        history = get_conversation_history(session, user_uuid)
        
        # AI応答を生成
        ai_text = generate_ai_response(user_data, message, list(history))
        
        # ★★★ 追加 ★★★ - 会話履歴をDBに保存
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()
        
        # 音声生成
        audio_url = ""
        if VOICEVOX_ENABLED:
            filename = f"voice_{user_uuid[:8]}_{int(time.time() * 1000)}.wav"
            audio_url = f'/voice/{filename}'
            threading.Thread(target=background_voice_generation, args=(ai_text, filename)).start()
        
        return app.response_class(response=f"{ai_text}|{audio_url}", status=200, mimetype='text/plain; charset=utf-8')
        
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントエラー: {e}", exc_info=True)
        session.rollback() # エラー時はロールバック
        return "Error: Internal server error", 500
    finally:
        session.close() # セッションを必ず閉じる

@app.route('/voice/<filename>')
def serve_voice(filename):
    # メモリキャッシュをまずチェック
    with voice_files_lock:
        if filename in voice_files:
            return app.response_class(response=voice_files[filename]['data'], mimetype='audio/wav')
    # ディスクから配信
    if os.path.exists(os.path.join(VOICE_DIR, filename)):
        return send_from_directory(VOICE_DIR, filename, mimetype='audio/wav')
    return "Error: Voice file not found or still generating", 404

# --- メイン実行部分 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0'
    logger.info(f"🚀 Flask アプリケーションを開始します (Deep Search Ver.): {host}:{port}")
    app.run(host=host, port=port, debug=False)
