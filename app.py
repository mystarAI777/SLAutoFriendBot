import os
import requests
import logging
import sys
import time
import threading
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq

# ログ設定を詳細に
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- 最適化設定 ---
VOICEVOX_MAX_TEXT_LENGTH = 50
VOICEVOX_FAST_TIMEOUT = 10
VOICEVOX_WORKERS = 2

# --- 音声ファイル保存設定 ---
VOICE_DIR = '/tmp/voices'

# --- 音声キャッシュ設定 ---
voice_cache = {}
CACHE_MAX_SIZE = 100
cache_lock = threading.Lock()

# --- Secret Fileからの設定読み込み ---
def get_secret(name):
    secret_file = f'/etc/secrets/{name}'
    try:
        with open(secret_file, 'r') as f:
            value = f.read().strip()
            logger.info(f"Secret FileからGOT {name[:4]}***を読み込みました。")
            return value
    except FileNotFoundError:
        logger.warning(f"Secret File '{secret_file}' が見つかりません。環境変数を試します。")
        return os.environ.get(name)
    except Exception as e:
        logger.error(f"{name} の読み込み中にエラー: {e}")
        return None

DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- 強化されたVOICEVOX接続テスト ---
VOICEVOX_URLS = [
    'http://localhost:50021',
    'http://127.0.0.1:50021',
    'http://voicevox-engine:50021',
    'http://voicevox:50021'
]

def check_system_processes():
    logger.info("🔍 システム診断開始")
    try:
        # (診断処理は省略)
        pass
    except Exception:
        pass

def find_working_voicevox_url():
    logger.info("🚀 VOICEVOX URL検索開始")
    check_system_processes()
    urls_to_test = [url for url in ([VOICEVOX_URL_FROM_ENV] + VOICEVOX_URLS) if url]
    
    for url in urls_to_test:
        try:
            logger.info(f"📡 テスト開始: {url}")
            # versionチェックは文字列でも辞書でも対応
            version_response = requests.get(f"{url}/version", timeout=5)
            if version_response.status_code != 200: continue
            
            # speakersチェックで成功とみなす
            speakers_response = requests.get(f"{url}/speakers", timeout=5)
            if speakers_response.status_code == 200:
                logger.info(f"🎯 VOICEVOX URL決定: {url}")
                return url
        except requests.exceptions.RequestException:
            continue
    logger.error("❌ 利用可能なVOICEVOX URLが見つかりません")
    return None

# --- 初期化処理 ---
WORKING_VOICEVOX_URL = find_working_voicevox_url()
logger.info(f"✅ VOICEVOX初期化完了: {WORKING_VOICEVOX_URL or '失敗'}")

# --- 必須変数のチェックとサービス初期化 ---
if not DATABASE_URL or not GROQ_API_KEY:
    logger.critical("FATAL: 必須環境変数が不足。")
    sys.exit(1)

app = Flask(__name__)
CORS(app)
groq_client = Groq(api_key=GROQ_API_KEY)
engine = create_engine(DATABASE_URL)
Base = declarative_base()

class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
    user_name = Column(String(255), nullable=False)
    interaction_count = Column(Integer, default=0)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# (ユーザーデータ処理、AI応答生成、キャッシュ機能などは変更なし)
class UserDataContainer:
    def __init__(self, user_uuid, user_name, interaction_count):
        self.user_uuid = user_uuid
        self.user_name = user_name
        self.interaction_count = interaction_count

def get_or_create_user(user_uuid, user_name):
    session = Session()
    try:
        user_memory = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        if user_memory: user_memory.interaction_count += 1
        else:
            user_memory = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
            session.add(user_memory)
        session.commit()
        return UserDataContainer(user_memory.user_uuid, user_memory.user_name, user_memory.interaction_count)
    finally: session.close()

def generate_ai_response(user_data, message):
    system_prompt = f"あなたは「もちこ」です。{user_data.user_name}さんと話します。40文字以内で親しみやすく返事してください。語尾「ですねぇ」「ますねぇ」、一人称「あてぃし」"
    completion = groq_client.chat.completions.create(
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": message or "こんにちは"}],
        model="llama3-8b-8192", temperature=0.8, max_tokens=80
    )
    return completion.choices[0].message.content.strip()

def get_cache_key(text, speaker_id): return f"{hash(text)}_{speaker_id}"
def get_cached_voice(text, speaker_id):
    with cache_lock: return voice_cache.get(get_cache_key(text, speaker_id))
def cache_voice(text, speaker_id, voice_data):
    with cache_lock:
        if len(voice_cache) >= CACHE_MAX_SIZE: del voice_cache[next(iter(voice_cache))]
        voice_cache[get_cache_key(text, speaker_id)] = voice_data

def generate_voice_fast(text, speaker_id=3):
    if not WORKING_VOICEVOX_URL: return None
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH: text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
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

# ★★★ ここからが大きな変更点 ★★★

def background_voice_generation(text, filename, speaker_id=3):
    """バックグラウンドで音声ファイルを生成・保存する関数"""
    logger.info(f"🎤 バックグラウンド音声生成開始: {filename}")
    voice_data = generate_voice_fast(text, speaker_id)
    
    if voice_data and len(voice_data) > 1000:
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            filepath = os.path.join(VOICE_DIR, filename)
            with open(filepath, 'wb') as f:
                f.write(voice_data)
            logger.info(f"✅ バックグラウンド音声保存成功: {filepath}")
        except Exception as e:
            logger.error(f"❌ バックグラウンド音声保存エラー: {e}")
    else:
        logger.warning(f"🎤 バックグラウンド音声生成失敗、またはデータ不足: {filename}")

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """非同期対応チャットエンドポイント"""
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        
        if not (user_uuid and user_name): return "Error: uuid and name required", 400
        
        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...)")
        
        # 1. テキストを先に生成
        user_data = get_or_create_user(user_uuid, user_name)
        ai_text = generate_ai_response(user_data, message)
        logger.info(f"🤖 AI応答: '{ai_text}'")
        
        audio_url = ""
        if WORKING_VOICEVOX_URL:
            # 2. 音声ファイル名とURLを先に決定
            timestamp = int(time.time() * 1000)
            filename = f"voice_{user_uuid[:8]}_{timestamp}.wav"
            audio_url = f'/voice/{filename}'
            
            # 3. 音声生成をバックグラウンドのスレッドで実行
            thread = threading.Thread(
                target=background_voice_generation,
                args=(ai_text, filename)
            )
            thread.start()
            logger.info(f"🚀 音声生成スレッドを開始しました。URL: {audio_url}")
        
        # 4. テキストと音声URLをすぐにクライアントへ返す
        response_text = f"{ai_text}|{audio_url}"
        logger.info(f"📤 即時レスポンス送信: Text='{ai_text}', URL='{audio_url}'")
        
        return app.response_class(response=response_text, status=200, mimetype='text/plain; charset=utf-8')
        
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントエラー: {e}", exc_info=True)
        return "Error: Internal server error", 500

# ★★★ ここまでが大きな変更点 ★★★

@app.route('/voice/<filename>')
def serve_voice(filename):
    """音声ファイル配信"""
    return send_from_directory(VOICE_DIR, filename)

@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'voicevox_url': WORKING_VOICEVOX_URL})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    app.run(host=host, port=port, debug=False, threaded=True)
