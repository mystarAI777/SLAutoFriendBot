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
VOICEVOX_FAST_TIMEOUT = 10     # タイムアウトを少し長く
VOICEVOX_WORKERS = 2           # 並列処理用ワーカー数

# 音声ファイル保存ディレクトリを作成
VOICE_DIR = '/tmp/voices'
os.makedirs(VOICE_DIR, exist_ok=True)

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

# --- 改良版VOICEVOX接続テスト ---
VOICEVOX_URLS = [
    'http://localhost:50021',
    'http://127.0.0.1:50021',
    'http://voicevox-engine:50021',
    'http://voicevox:50021'
]

def find_working_voicevox_url():
    """改良版 - VOICEVOX URL検索（より詳細なテスト）"""
    urls_to_test = []
    if VOICEVOX_URL_FROM_ENV:
        urls_to_test.insert(0, VOICEVOX_URL_FROM_ENV)
    urls_to_test.extend([url for url in VOICEVOX_URLS if url not in urls_to_test])
    
    logger.info(f"🔍 VOICEVOX URL検索開始: {len(urls_to_test)}個のURLをテスト")
    
    for i, url in enumerate(urls_to_test, 1):
        logger.info(f"📡 ({i}/{len(urls_to_test)}) テスト中: {url}")
        try:
            # Step 1: バージョンチェック
            version_response = requests.get(f"{url}/version", timeout=5)
            if version_response.status_code != 200:
                logger.warning(f"❌ バージョンチェック失敗: {version_response.status_code}")
                continue
                
            version_info = version_response.json()
            version = version_info.get('version', 'unknown')
            logger.info(f"✅ バージョン確認成功: v{version}")
            
            # Step 2: スピーカーリストチェック
            speakers_response = requests.get(f"{url}/speakers", timeout=5)
            if speakers_response.status_code != 200:
                logger.warning(f"❌ スピーカーリスト取得失敗: {speakers_response.status_code}")
                continue
                
            speakers = speakers_response.json()
            logger.info(f"✅ スピーカー確認成功: {len(speakers)}個")
            
            # Step 3: 実際の音声合成テスト
            test_text = "テスト"
            test_query_response = requests.post(
                f"{url}/audio_query",
                params={'text': test_text, 'speaker': 3},
                timeout=5
            )
            if test_query_response.status_code != 200:
                logger.warning(f"❌ 音声クエリテスト失敗: {test_query_response.status_code}")
                continue
                
            logger.info(f"✅ 音声クエリテスト成功")
            
            # 全テスト通過
            logger.info(f"🎯 VOICEVOX URL決定: {url}")
            return url
                
        except requests.exceptions.Timeout:
            logger.warning(f"⏰ タイムアウト: {url}")
            continue
        except requests.exceptions.ConnectionError:
            logger.warning(f"🔌 接続エラー: {url}")
            continue
        except Exception as e:
            logger.error(f"❌ 予期しないエラー ({url}): {e}")
            continue
    
    logger.error("❌ 利用可能なVOICEVOX URLが見つかりません")
    return None

# VOICEVOX接続テスト実行
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

# --- 修正版 - 確実な音声生成関数 ---
def generate_voice_fast(text, speaker_id=3):
    """修正版 - より確実な音声生成"""
    if not WORKING_VOICEVOX_URL:
        logger.warning("🚫 VOICEVOX_URL未設定 - 音声生成スキップ")
        return None
    
    # テキスト長制限
    original_length = len(text)
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH:
        text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
        logger.info(f"📝 テキスト短縮: {original_length} → {len(text)}文字")
    
    # キャッシュチェック
    cached_voice = get_cached_voice(text, speaker_id)
    if cached_voice:
        logger.info(f"🚀 キャッシュヒット: '{text[:20]}...' ({len(cached_voice)}bytes)")
        return cached_voice
    
    try:
        logger.info(f"🎵 音声合成開始: '{text}' (speaker:{speaker_id})")
        start_time = time.time()
        
        # ステップ1: audio_query
        query_url = f"{WORKING_VOICEVOX_URL}/audio_query"
        query_params = {'text': text, 'speaker': speaker_id}
        logger.info(f"📤 Query送信: {query_url}")
        
        query_response = requests.post(
            query_url,
            params=query_params,
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        
        logger.info(f"📥 Query応答: {query_response.status_code}")
        if query_response.status_code != 200:
            logger.error(f"❌ Query失敗: {query_response.status_code} - {query_response.text}")
            return None
            
        query_data = query_response.json()
        logger.info(f"📋 Query成功")
        
        # ステップ2: synthesis
        synthesis_url = f"{WORKING_VOICEVOX_URL}/synthesis"
        synthesis_params = {'speaker': speaker_id}
        logger.info(f"📤 Synthesis送信: {synthesis_url}")
        
        synthesis_response = requests.post(
            synthesis_url,
            params=synthesis_params,
            json=query_data,
            timeout=VOICEVOX_FAST_TIMEOUT * 2,
            headers={'Content-Type': 'application/json'}
        )
        
        logger.info(f"📥 Synthesis応答: {synthesis_response.status_code}")
        if synthesis_response.status_code != 200:
            logger.error(f"❌ Synthesis失敗: {synthesis_response.status_code} - {synthesis_response.text}")
            return None
        
        voice_data = synthesis_response.content
        if not voice_data or len(voice_data) < 1000:
            logger.error(f"❌ 無効な音声データ: {len(voice_data) if voice_data else 0}bytes")
            return None
        
        # 処理時間ログ
        elapsed = time.time() - start_time
        logger.info(f"✅ 音声合成成功: {elapsed:.2f}秒, {len(voice_data)}bytes")
        
        # キャッシュに保存
        cache_voice(text, speaker_id, voice_data)
        
        return voice_data
        
    except requests.exceptions.Timeout:
        logger.error(f"⏰ 音声合成タイムアウト: '{text}'")
        return None
    except requests.exceptions.RequestException as req_error:
        logger.error(f"🌐 リクエストエラー: {req_error}")
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
        'voicevox_url': WORKING_VOICEVOX_URL,
        'optimizations': {
            'text_limit': VOICEVOX_MAX_TEXT_LENGTH,
            'fast_timeout': VOICEVOX_FAST_TIMEOUT,
            'cache_size': len(voice_cache),
            'workers': VOICEVOX_WORKERS
        }
    })

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """修正版 - LSLチャットエンドポイント（音声URL確実生成版）"""
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        
        if not (user_uuid and user_name):
            logger.error("❌ 必須パラメータ不足: uuid or name")
            return "Error: uuid and name required", 400
        
        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message[:30]}...'")
        
        # 高速ユーザーデータ取得
        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data:
            logger.error("❌ ユーザーデータ取得失敗")
            return "Error: User data failed", 500
        
        # AI応答生成（高速）
        ai_text = generate_ai_response(user_data, message)
        logger.info(f"🤖 AI応答: '{ai_text}'")
        
        # VOICEVOX利用不可の場合
        if not WORKING_VOICEVOX_URL:
            logger.warning("🚫 VOICEVOX利用不可 - テキストのみ返却")
            response_text = f"{ai_text}|"
            return app.response_class(
                response=response_text, 
                status=200, 
                mimetype='text/plain; charset=utf-8'
            )
        
        # 音声生成実行
        logger.info(f"🎵 音声生成開始: VOICEVOX_URL={WORKING_VOICEVOX_URL}")
        voice_data = generate_voice_fast(ai_text, speaker_id=3)
        
        audio_url = ""
        if voice_data and len(voice_data) > 0:
            try:
                # ユニークなファイル名生成
                timestamp = int(time.time() * 1000)
                filename = f"voice_{user_uuid[:8]}_{timestamp}.wav"
                filepath = os.path.join(VOICE_DIR, filename)
                
                # 音声ファイル保存
                with open(filepath, 'wb') as f:
                    f.write(voice_data)
                
                # ファイル保存確認
                if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                    audio_url = f'/voice/{filename}'
                    logger.info(f"✅ 音声ファイル保存成功: {filepath} -> {audio_url}")
                    logger.info(f"📁 ファイルサイズ: {os.path.getsize(filepath)}bytes")
                else:
                    logger.error(f"❌ 音声ファイル保存検証失敗: {filepath}")
                    
            except Exception as file_error:
                logger.error(f"❌ ファイル保存エラー: {file_error}")
        else:
            logger.warning(f"❌ 音声データ生成失敗")
        
        # 最終レスポンス生成
        response_text = f"{ai_text}|{audio_url}"
        logger.info(f"📤 最終レスポンス: '{ai_text}' | '{audio_url}'")
        
        return app.response_class(
            response=response_text, 
            status=200, 
            mimetype='text/plain; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"❌ チャットエラー: {e}")
        import traceback
        traceback.print_exc()
        return "Error: Internal server error", 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    """音声ファイル配信（改良版）"""
    try:
        filepath = os.path.join(VOICE_DIR, filename)
        if not os.path.exists(filepath):
            logger.error(f"❌ 音声ファイル不存在: {filepath}")
            return "File not found", 404
            
        logger.info(f"📁 音声ファイル配信: {filename} ({os.path.getsize(filepath)}bytes)")
        return send_from_directory(VOICE_DIR, filename)
    except Exception as e:
        logger.error(f"❌ 音声ファイル配信エラー: {e}")
        return "File not found", 404

@app.route('/health')
def health_check():
    """ヘルスチェック（詳細版）"""
    health_status = {
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'database': 'connected' if DATABASE_URL else 'unavailable',
        'groq_api': 'available' if groq_client else 'unavailable',
        'voicevox': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
        'voicevox_url': WORKING_VOICEVOX_URL,
        'voice_directory': {
            'path': VOICE_DIR,
            'exists': os.path.exists(VOICE_DIR),
            'writable': os.access(VOICE_DIR, os.W_OK) if os.path.exists(VOICE_DIR) else False
        },
        'cache_stats': {
            'size': len(voice_cache),
            'max_size': CACHE_MAX_SIZE
        },
        'optimization': {
            'text_limit': VOICEVOX_MAX_TEXT_LENGTH,
            'timeout': VOICEVOX_FAST_TIMEOUT,
            'workers': VOICEVOX_WORKERS
        }
    }
    
    # VOICEVOX詳細チェック
    if WORKING_VOICEVOX_URL:
        try:
            version_response = requests.get(f"{WORKING_VOICEVOX_URL}/version", timeout=3)
            if version_response.status_code == 200:
                health_status['voicevox_version'] = version_response.json()
        except:
            health_status['voicevox_version'] = 'check_failed'
    
    return jsonify(health_status)

@app.route('/cache/clear', methods=['POST'])
def clear_cache():
    """音声キャッシュクリア"""
    with cache_lock:
        cache_size = len(voice_cache)
        voice_cache.clear()
    logger.info(f"🗑️ 音声キャッシュクリア: {cache_size}個削除")
    return jsonify({'message': f'Cache cleared: {cache_size} items'})

# --- 詳細デバッグエンドポイント ---
@app.route('/debug/voicevox')
def debug_voicevox():
    """VOICEVOX詳細デバッグ情報"""
    debug_info = {
        'current_working_url': WORKING_VOICEVOX_URL,
        'env_url': VOICEVOX_URL_FROM_ENV,
        'voice_directory': {
            'path': VOICE_DIR,
            'exists': os.path.exists(VOICE_DIR),
            'writable': os.access(VOICE_DIR, os.W_OK) if os.path.exists(VOICE_DIR) else False,
            'files_count': len(os.listdir(VOICE_DIR)) if os.path.exists(VOICE_DIR) else 0
        },
        'test_results': {}
    }
    
    # 全URLを再テスト
    test_urls = [VOICEVOX_URL_FROM_ENV] if VOICEVOX_URL_FROM_ENV else []
    test_urls.extend(VOICEVOX_URLS)
    
    for url in test_urls:
        if not url:
            continue
            
        test_result = {
            'url': url,
            'version_test': 'failed',
            'speakers_test': 'failed',
            'synthesis_test': 'failed',
            'error': None
        }
        
        try:
            # バージョンテスト
            version_response = requests.get(f"{url}/version", timeout=3)
            if version_response.status_code == 200:
                test_result['version_test'] = 'success'
                test_result['version_info'] = version_response.json()
            
            # スピーカーテスト
            speakers_response = requests.get(f"{url}/speakers", timeout=3)
            if speakers_response.status_code == 200:
                test_result['speakers_test'] = 'success'
                test_result['speakers_count'] = len(speakers_response.json())
            
            # 音声合成テスト
            query_response = requests.post(
                f"{url}/audio_query",
                params={'text': 'テスト', 'speaker': 3},
                timeout=5
            )
            if query_response.status_code == 200:
                test_result['synthesis_test'] = 'success'
                
        except Exception as e:
            test_result['error'] = str(e)
        
        debug_info['test_results'][url] = test_result
    
    return jsonify(debug_info)

@app.route('/debug/voice_test', methods=['POST'])
def debug_voice_test():
    """音声生成完全テスト"""
    data = request.json or {}
    test_text = data.get('text', 'テストですわ')
    speaker_id = data.get('speaker', 3)
    
    result = {
        'test_text': test_text,
        'speaker_id': speaker_id,
        'working_url': WORKING_VOICEVOX_URL,
        'voice_directory': VOICE_DIR,
        'steps': {}
    }
    
    if not WORKING_VOICEVOX_URL:
        result['error'] = 'VOICEVOX URL not available'
        return jsonify(result)
    
    try:
        # Step 1: 音声生成
        result['steps']['voice_generation'] = 'starting'
        voice_data = generate_voice_fast(test_text, speaker_id)
        
        if voice_data:
            result['steps']['voice_generation'] = 'success'
            result['voice_data_size'] = len(voice_data)
            
            # Step 2: ファイル保存
            result['steps']['file_save'] = 'starting'
            test_filename = f"test_voice_{int(time.time())}.wav"
            test_filepath = os.path.join(VOICE_DIR, test_filename)
            
            with open(test_filepath, 'wb') as f:
                f.write(voice_data)
            
            # Step 3: ファイル確認
            if os.path.exists(test_filepath) and os.path.getsize(test_filepath) > 0:
                result['steps']['file_save'] = 'success'
                result['test_file_path'] = test_filepath
                result['test_file_size'] = os.path.getsize(test_filepath)
                result['test_file_url'] = f'/voice/{test_filename}'
                result['success'] = True
            else:
                result['steps']['file_save'] = 'failed'
                result['error'] = 'File save verification failed'
        else:
            result['steps']['voice_generation'] = 'failed'
            result['error'] = 'Voice generation returned None'
            
    except Exception as e:
        result['error'] = str(e)
        result['success'] = False
    
    return jsonify(result)

# --- アプリケーション実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    
    logger.info(f"🚀 もちこAI最適化版起動")
    logger.info(f"📍 {host}:{port}")
    logger.info(f"🗄️ DB: {'OK' if DATABASE_URL else 'NG'}")
    logger.info(f"🤖 Groq: {'OK' if groq_client else 'NG'}")
    logger.info(f"🎵 VOICEVOX: {'OK' if WORKING_VOICEVOX_URL else 'NG'}")
    if WORKING_VOICEVOX_URL:
        logger.info(f"🎯 VOICEVOX URL: {WORKING_VOICEVOX_URL}")
    logger.info(f"📁 音声ディレクトリ: {VOICE_DIR}")
    logger.info(f"⚡ 最適化設定:")
    logger.info(f"   - テキスト制限: {VOICEVOX_MAX_TEXT_LENGTH}文字")
    logger.info(f"   - タイムアウト: {VOICEVOX_FAST_TIMEOUT}秒")
    logger.info(f"   - キャッシュサイズ: {CACHE_MAX_SIZE}個")
    logger.info(f"   - ワーカー数: {VOICEVOX_WORKERS}個")
    
    app.run(host=host, port=port, debug=False, threaded=True)
