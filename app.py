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
# openaiはインポートリストにありますが使用されていないため、コメントアウトまたは削除も可能です
# from openai import OpenAI 

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

# --- 音声ファイル保存設定 (修正点) ---
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
        ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        voicevox_processes = [line for line in ps_result.stdout.split('\n') if 'voicevox' in line.lower()]
        if voicevox_processes:
            logger.info(f"🔍 VOICEVOXプロセス発見: {len(voicevox_processes)}個")
            for proc in voicevox_processes[:3]:
                logger.info(f"   {proc}")
        else:
            logger.warning("🔍 VOICEVOXプロセスが見つかりません")
    except Exception as e:
        logger.error(f"🔍 プロセス確認エラー: {e}")

# --- find_working_voicevox_url (修正済みの関数) ---
def find_working_voicevox_url():
    """強化され、簡略化されたVOICEVOX URL検索"""
    logger.info("🚀 VOICEVOX URL検索開始")
    check_system_processes()
    
    urls_to_test = []
    if VOICEVOX_URL_FROM_ENV:
        urls_to_test.insert(0, VOICEVOX_URL_FROM_ENV)
    urls_to_test.extend([url for url in VOICEVOX_URLS if url not in urls_to_test])
    
    logger.info(f"🧪 テスト対象URL: {len(urls_to_test)}個")
    
    for i, url in enumerate(urls_to_test, 1):
        logger.info(f"📡 ({i}/{len(urls_to_test)}) テスト開始: {url}")
        
        try:
            # Step 1: バージョンチェック
            logger.info(f"   ステップ1: GET {url}/version")
            version_response = requests.get(f"{url}/version", timeout=5)
            if version_response.status_code != 200:
                logger.warning(f"   ❌ バージョンチェック失敗: {version_response.status_code}")
                continue # 次のURLへ

            version_info = version_response.json()
            version = version_info if isinstance(version_info, str) else version_info.get('version', 'unknown')
            logger.info(f"   ✅ バージョン: v{version}")
            
            # Step 2: スピーカーリストチェック
            logger.info(f"   ステップ2: GET {url}/speakers")
            speakers_response = requests.get(f"{url}/speakers", timeout=5)
            if speakers_response.status_code == 200:
                speakers = speakers_response.json()
                logger.info(f"   ✅ スピーカーリスト取得成功 ({len(speakers)}個)")
                logger.info(f"🎯 VOICEVOX URL決定: {url}")
                return url # ★★★ ここで成功とみなし、URLを返して終了 ★★★
            else:
                logger.warning(f"   ❌ スピーカーリスト取得失敗: {speakers_response.status_code}")
                
        except requests.exceptions.Timeout as e:
            logger.warning(f"   ⏰ タイムアウト: {e}")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"   🔌 接続エラー: {e}")
        except Exception as e:
            logger.error(f"   ❌ 予期しないエラー: {e}")
            
    logger.error("❌ 利用可能なVOICEVOX URLが見つかりません")
    return None

# --- 初期化処理 ---
logger.info("=" * 50)
logger.info("VOICEVOX初期化開始")
WORKING_VOICEVOX_URL = find_working_voicevox_url()
if WORKING_VOICEVOX_URL:
    logger.info(f"✅ VOICEVOX初期化成功: {WORKING_VOICEVOX_URL}")
else:
    logger.error("❌ VOICEVOX初期化失敗")
logger.info("=" * 50)

# --- 必須変数のチェック ---
if not DATABASE_URL or not GROQ_API_KEY:
    logger.critical("FATAL: 必須環境変数が不足しています。")
    sys.exit(1)

# --- Flaskアプリケーション初期化 ---
app = Flask(__name__)
CORS(app, origins=["*"], methods=["GET", "POST", "OPTIONS"])

# --- サービス初期化 ---
groq_client = None
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    groq_client.chat.completions.create(
        messages=[{"role": "user", "content": "Hi"}], 
        model="llama3-8b-8192", max_tokens=3
    )
    logger.info("✅ Groq API - OK")
except Exception as e:
    logger.error(f"❌ Groq API初期化失敗: {e}")

# --- データベース設定 ---
Base = declarative_base()
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
    user_name = Column(String(255), nullable=False)
    interaction_count = Column(Integer, default=0)

try:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info("✅ データベース - OK")
except Exception as e:
    logger.critical(f"FATAL: データベース接続失敗: {e}")
    sys.exit(1)

# --- ユーザーデータ処理 ---
class UserDataContainer:
    def __init__(self, user_uuid, user_name, interaction_count):
        self.user_uuid = user_uuid
        self.user_name = user_name
        self.interaction_count = interaction_count

def get_or_create_user(user_uuid, user_name):
    session = Session()
    try:
        user_memory = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        if user_memory:
            user_memory.interaction_count += 1
        else:
            user_memory = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
            session.add(user_memory)
        session.commit()
        return UserDataContainer(
            user_uuid=user_memory.user_uuid,
            user_name=user_memory.user_name,
            interaction_count=user_memory.interaction_count
        )
    finally:
        session.close()

# --- AI応答生成 ---
def generate_ai_response(user_data, message):
    if not groq_client: return f"{user_data.user_name}さん、こんにちは！"
    system_prompt = f"あなたは「もちこ」です。{user_data.user_name}さんと話します。40文字以内で親しみやすく返事してください。語尾「ですわ」「ますわ」、一人称「あてぃし」"
    try:
        completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": message or "こんにちは"}], 
            model="llama3-8b-8192", temperature=0.8, max_tokens=80
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答エラー: {e}")
        return f"{user_data.user_name}さん、ちょっと考え中ですわ！"

# --- 音声キャッシュ機能 ---
def get_cache_key(text, speaker_id): return f"{hash(text)}_{speaker_id}"
def get_cached_voice(text, speaker_id):
    with cache_lock: return voice_cache.get(get_cache_key(text, speaker_id))
def cache_voice(text, speaker_id, voice_data):
    with cache_lock:
        if len(voice_cache) >= CACHE_MAX_SIZE:
            del voice_cache[next(iter(voice_cache))]
        voice_cache[get_cache_key(text, speaker_id)] = voice_data

# --- 強化された音声生成 (修正済みの関数) ---
def generate_voice_fast(text, speaker_id=3):
    if not WORKING_VOICEVOX_URL:
        logger.warning("🚫 VOICEVOX利用不可 - 音声生成スキップ")
        return None
        
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH:
        text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
    
    if cached_voice := get_cached_voice(text, speaker_id):
        logger.info(f"🚀 キャッシュヒット: '{text[:20]}...'")
        return cached_voice
    
    logger.info(f"🎵 音声合成開始: '{text}' (speaker:{speaker_id})")
    start_time = time.time()
    
    try:
        # Step 1: audio_query
        logger.info("   -> Step 1/2: audio_query")
        query_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/audio_query", 
            params={'text': text, 'speaker': speaker_id}, 
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        if query_response.status_code != 200:
            logger.error(f"❌ Query失敗: {query_response.status_code} {query_response.text[:200]}")
            return None
        
        # Step 2: synthesis
        logger.info("   -> Step 2/2: synthesis")
        synthesis_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/synthesis", 
            params={'speaker': speaker_id}, 
            json=query_response.json(), 
            timeout=VOICEVOX_FAST_TIMEOUT * 4  # ★★★ タイムアウトを40秒に延長 ★★★
        )
        if synthesis_response.status_code != 200:
            logger.error(f"❌ Synthesis失敗: {synthesis_response.status_code} {synthesis_response.text[:200]}")
            return None
            
        voice_data = synthesis_response.content
        logger.info(f"✅ 音声合成成功: {time.time() - start_time:.2f}秒, {len(voice_data)}bytes")
        cache_voice(text, speaker_id, voice_data)
        return voice_data
        
    except Exception as e:
        logger.error(f"❌ 音声合成エラー: {e}")
        return None

# --- エンドポイント ---
@app.route('/')
def index():
    return jsonify({
        'service': 'もちこ AI Assistant (修正版)',
        'status': 'running',
        'voicevox_status': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
        'voicevox_url': WORKING_VOICEVOX_URL
    })

# --- chat_lsl (修正済みの関数) ---
@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        
        if not (user_uuid and user_name): return "Error: uuid and name required", 400
        
        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message[:30]}...'")
        user_data = get_or_create_user(user_uuid, user_name)
        ai_text = generate_ai_response(user_data, message)
        logger.info(f"🤖 AI応答: '{ai_text}'")
        
        voice_data = generate_voice_fast(ai_text, speaker_id=3)
        audio_url = ""
        
        if voice_data and len(voice_data) > 1000: # 1KB未満は無効なデータと見なす
            try:
                # ディレクトリが存在することを確認・なければ作成
                os.makedirs(VOICE_DIR, exist_ok=True)
                
                timestamp = int(time.time() * 1000)
                filename = f"voice_{user_uuid[:8]}_{timestamp}.wav"
                filepath = os.path.join(VOICE_DIR, filename)
                
                with open(filepath, 'wb') as f:
                    f.write(voice_data)
                
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    audio_url = f'/voice/{filename}'
                    logger.info(f"✅ 音声ファイル保存成功: {filepath}")
                else:
                    logger.error(f"❌ 音声ファイル保存検証失敗: {filepath}")
            except Exception as file_error:
                logger.error(f"❌ ファイル保存エラー: {file_error}")
        else:
            logger.warning("音声データ生成失敗、またはデータサイズが不十分です。")

        response_text = f"{ai_text}|{audio_url}"
        return app.response_class(response=response_text, status=200, mimetype='text/plain; charset=utf-8')
        
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントエラー: {e}", exc_info=True)
        return "Error: Internal server error", 500

# --- serve_voice (修正済みの関数) ---
@app.route('/voice/<filename>')
def serve_voice(filename):
    """音声ファイル配信"""
    try:
        logger.info(f"📁 音声ファイル配信要求: {filename} from {VOICE_DIR}")
        if not os.path.exists(os.path.join(VOICE_DIR, filename)):
             logger.error(f"❌ 配信ファイル不存在: {filename}")
             return "File not found", 404
        return send_from_directory(VOICE_DIR, filename)
    except Exception as e:
        logger.error(f"❌ 音声ファイル配信エラー: {e}")
        return "File not found", 404

# --- ヘルスチェックとデバッグ用エンドポイント ---
@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'voicevox_url': WORKING_VOICEVOX_URL})

@app.route('/debug/voicevox_retry')
def debug_voicevox_retry():
    global WORKING_VOICEVOX_URL
    WORKING_VOICEVOX_URL = find_working_voicevox_url()
    return jsonify({'result': 'success' if WORKING_VOICEVOX_URL else 'failed', 'working_url': WORKING_VOICEVOX_URL})

# --- アプリケーション実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    
    logger.info("🚀 もちこAI (修正版) 起動")
    logger.info(f"🌍 http://{host}:{port}")
    logger.info(f"🎵 VOICEVOX: {'OK' if WORKING_VOICEVOX_URL else 'NG'} ({WORKING_VOICEVOX_URL or 'N/A'})")
    
    app.run(host=host, port=port, debug=False, threaded=True)
