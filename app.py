import os
import requests
import logging
import sys
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq
from openai import OpenAI

# ログ設定
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 最適化設定 ---
# VOICEVOX処理最適化
VOICEVOX_MAX_TEXT_LENGTH = 50  # テキスト長制限（短縮）
VOICEVOX_FAST_TIMEOUT = 5      # 高速タイムアウト（短縮）
VOICEVOX_WORKERS = 2           # 並列処理用ワーカー数

# 音声キャッシュ（簡易版）
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

# --- 最適化されたVOICEVOX接続テスト ---
VOICEVOX_URLS = [
    'http://localhost:50021',
    'http://127.0.0.1:50021',
    'http://voicevox-engine:50021',
    'http://voicevox:50021'
]

def find_working_voicevox_url():
    """最適化されたVOICEVOX URL検索（高速化）"""
    urls_to_test = []
    if VOICEVOX_URL_FROM_ENV:
        urls_to_test.insert(0, VOICEVOX_URL_FROM_ENV)
    urls_to_test.extend([url for url in VOICEVOX_URLS if url not in urls_to_test])
    
    for url in urls_to_test:
        try:
            logger.info(f"🔍 VOICEVOX高速テスト: {url}")
            # 超短時間タイムアウトでテスト
            response = requests.get(f"{url}/version", timeout=2)
            if response.status_code == 200:
                version_info = response.json()
                logger.info(f"✅ VOICEVOX接続成功: {url}, v{version_info.get('version', 'unknown')}")
                
                # 簡易スピーカーテスト（高速化）
                try:
                    speakers_response = requests.get(f"{url}/speakers", timeout=1)
                    if speakers_response.status_code == 200:
                        speakers = speakers_response.json()
                        logger.info(f"📢 スピーカー数: {len(speakers)}個")
                except:
                    pass  # スピーカー情報取得失敗は無視
                
                return url
        except:
            continue
    
    logger.error("❌ VOICEVOX利用不可 - 音声機能無効")
    return None

WORKING_VOICEVOX_URL = find_working_voicevox_url()

# --- 必須変数のチェック ---
if not DATABASE_URL or not GROQ_API_KEY:
    logger.critical("FATAL: 必須環境変数が不足しています。")
    sys.exit(1)

# --- Flaskアプリケーション初期化 ---
app = Flask(__name__)
CORS(app, origins=["*"], methods=["GET", "POST", "OPTIONS"])

# --- 最適化されたサービス初期化 ---
groq_client = None
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    # APIキー検証（高速）
    test_response = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": "Hi"}], 
        model="llama3-8b-8192", 
        max_tokens=3
    )
    logger.info("✅ Groq API - OK")
except Exception as e:
    logger.error(f"❌ Groq API初期化失敗: {e}")

# --- データベース設定（簡素化） ---
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

class UserDataContainer:
    def __init__(self, user_uuid, user_name, interaction_count):
        self.user_uuid = user_uuid
        self.user_name = user_name
        self.interaction_count = interaction_count

def get_or_create_user(user_uuid, user_name):
    """最適化されたユーザーデータ取得"""
    session = Session()
    try:
        user_memory = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        if user_memory:
            user_memory.interaction_count += 1
        else:
            user_memory = UserMemory(
                user_uuid=user_uuid, 
                user_name=user_name, 
                interaction_count=1
            )
            session.add(user_memory)
        session.commit()
        return UserDataContainer(
            user_uuid=user_memory.user_uuid,
            user_name=user_memory.user_name,
            interaction_count=user_memory.interaction_count
        )
    except Exception as e:
        logger.error(f"ユーザーデータエラー: {e}")
        session.rollback()
        return UserDataContainer(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
    finally:
        session.close()

def generate_ai_response(user_data, message):
    """最適化されたAI応答生成"""
    if not groq_client:
        return f"{user_data.user_name}さん、こんにちは！"
    
    # 超シンプルプロンプト（高速化）
    system_prompt = f"あなたは「もちこ」です。{user_data.user_name}さんと話します。40文字以内で親しみやすく返事してください。語尾「ですわ」「ますわ」、一人称「あてぃし」"
    
    try:
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt}, 
                {"role": "user", "content": message or "こんにちは"}
            ], 
            model="llama3-8b-8192",
            temperature=0.8,
            max_tokens=80  # トークン数削減（高速化）
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答エラー: {e}")
        return f"{user_data.user_name}さん、ちょっと考え中ですわ！"

# --- 音声キャッシュ機能 ---
def get_cache_key(text, speaker_id):
    """音声キャッシュキー生成"""
    return f"{hash(text)}_{speaker_id}"

def get_cached_voice(text, speaker_id):
    """キャッシュから音声取得"""
    with cache_lock:
        key = get_cache_key(text, speaker_id)
        return voice_cache.get(key)

def cache_voice(text, speaker_id, voice_data):
    """音声をキャッシュに保存"""
    with cache_lock:
        if len(voice_cache) >= CACHE_MAX_SIZE:
            # 古いキャッシュを削除（FIFO）
            oldest_key = next(iter(voice_cache))
            del voice_cache[oldest_key]
        
        key = get_cache_key(text, speaker_id)
        voice_cache[key] = voice_data

# --- 超高速VOICEVOX音声生成 ---
def generate_voice_fast(text, speaker_id=3):
    """最適化された高速音声生成"""
    if not WORKING_VOICEVOX_URL:
        return None
    
    # テキスト長制限（大幅短縮）
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH:
        text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
        logger.debug(f"テキスト短縮: {len(text)}文字")
    
    # キャッシュチェック
    cached_voice = get_cached_voice(text, speaker_id)
    if cached_voice:
        logger.info(f"🚀 キャッシュヒット: '{text[:20]}...'")
        return cached_voice
    
    try:
        logger.debug(f"🎵 音声合成開始: '{text[:20]}...'")
        start_time = time.time()
        
        # ステップ1: audio_query（超高速）
        query_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/audio_query",
            params={'text': text, 'speaker': speaker_id},
            timeout=VOICEVOX_FAST_TIMEOUT,
            headers={'Connection': 'close'}  # 接続最適化
        )
        query_response.raise_for_status()
        query_data = query_response.json()
        
        # ステップ2: synthesis（超高速）
        synthesis_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/synthesis",
            params={'speaker': speaker_id},
            json=query_data,
            timeout=VOICEVOX_FAST_TIMEOUT * 2,  # synthesisは少し長め
            headers={'Connection': 'close', 'Content-Type': 'application/json'}
        )
        synthesis_response.raise_for_status()
        
        voice_data = synthesis_response.content
        if not voice_data or len(voice_data) < 1000:
            raise ValueError(f"無効な音声データ: {len(voice_data)}bytes")
        
        # 処理時間ログ
        elapsed = time.time() - start_time
        logger.info(f"✅ 音声合成成功: {elapsed:.2f}秒, {len(voice_data)}bytes")
        
        # キャッシュに保存
        cache_voice(text, speaker_id, voice_data)
        
        return voice_data
        
    except requests.exceptions.Timeout:
        logger.warning(f"⏰ 音声合成タイムアウト({VOICEVOX_FAST_TIMEOUT}s): '{text[:20]}...'")
        return None
    except Exception as e:
        logger.error(f"❌ 音声合成エラー: {e}")
        return None

# --- 並列処理エンドポイント ---
executor = ThreadPoolExecutor(max_workers=VOICEVOX_WORKERS)

@app.route('/')
def index():
    return jsonify({
        'service': 'もちこ AI Assistant (最適化版)',
        'status': 'running',
        'voicevox': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
        'optimizations': {
            'text_limit': VOICEVOX_MAX_TEXT_LENGTH,
            'fast_timeout': VOICEVOX_FAST_TIMEOUT,
            'cache_size': len(voice_cache),
            'workers': VOICEVOX_WORKERS
        }
    })

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """最適化されたLSLチャットエンドポイント"""
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        
        if not (user_uuid and user_name):
            return "Error: uuid and name required", 400
        
        # 高速ユーザーデータ取得
        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data:
            return "Error: User data failed", 500
        
        # AI応答生成（高速）
        ai_response = generate_ai_response(user_data, message)
        
        # 音声生成（並列処理可能）
        voice_data = generate_voice_fast(ai_response, speaker_id=3)
        
        audio_url_part = ""
        if voice_data:
            filename = f"voice_{user_uuid}_{int(time.time() * 1000)}.wav"
            filepath = os.path.join('/tmp', filename)
            with open(filepath, 'wb') as f:
                f.write(voice_data)
            audio_url_part = f'/voice/{filename}'
            logger.debug(f"音声ファイル保存: {audio_url_part}")
        
        response_text = f"{ai_response}|{audio_url_part}"
        return app.response_class(
            response=response_text, 
            status=200, 
            mimetype='text/plain; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"チャットエラー: {e}")
        return "Error: Internal server error", 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    """音声ファイル配信"""
    try:
        return send_from_directory('/tmp', filename)
    except Exception as e:
        logger.error(f"音声ファイル配信エラー: {e}")
        return "File not found", 404

@app.route('/health')
def health_check():
    """ヘルスチェック（最適化情報付き）"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'database': 'connected' if DATABASE_URL else 'unavailable',
        'groq_api': 'available' if groq_client else 'unavailable',
        'voicevox': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
        'cache_stats': {
            'size': len(voice_cache),
            'max_size': CACHE_MAX_SIZE
        },
        'optimization': {
            'text_limit': VOICEVOX_MAX_TEXT_LENGTH,
            'timeout': VOICEVOX_FAST_TIMEOUT,
            'workers': VOICEVOX_WORKERS
        }
    })

@app.route('/cache/clear', methods=['POST'])
def clear_cache():
    """音声キャッシュクリア（デバッグ用）"""
    with cache_lock:
        cache_size = len(voice_cache)
        voice_cache.clear()
    logger.info(f"🗑️ 音声キャッシュクリア: {cache_size}個削除")
    return jsonify({'message': f'Cache cleared: {cache_size} items'})

# --- アプリケーション実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    
    logger.info(f"🚀 もちこAI最適化版起動")
    logger.info(f"📍 {host}:{port}")
    logger.info(f"🗄️ DB: {'OK' if DATABASE_URL else 'NG'}")
    logger.info(f"🤖 Groq: {'OK' if groq_client else 'NG'}")
    logger.info(f"🎵 VOICEVOX: {'OK' if WORKING_VOICEVOX_URL else 'NG'}")
    logger.info(f"⚡ 最適化設定:")
    logger.info(f"   - テキスト制限: {VOICEVOX_MAX_TEXT_LENGTH}文字")
    logger.info(f"   - 高速タイムアウト: {VOICEVOX_FAST_TIMEOUT}秒")
    logger.info(f"   - キャッシュサイズ: {CACHE_MAX_SIZE}個")
    logger.info(f"   - ワーカー数: {VOICEVOX_WORKERS}個")
    
    app.run(host=host, port=port, debug=False, threaded=True)
