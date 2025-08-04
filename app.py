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
from openai import OpenAI

# ログ設定を詳細に
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- 最適化設定 ---
VOICEVOX_MAX_TEXT_LENGTH = 50
VOICEVOX_FAST_TIMEOUT = 10  # タイムアウトを長く
VOICEVOX_WORKERS = 2

# 音声キャッシュ
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
    """システムプロセスとポートの確認"""
    logger.info("🔍 システム診断開始")
    
    try:
        # プロセス確認
        ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        voicevox_processes = [line for line in ps_result.stdout.split('\n') if 'voicevox' in line.lower()]
        if voicevox_processes:
            logger.info(f"🔍 VOICEVOXプロセス発見: {len(voicevox_processes)}個")
            for proc in voicevox_processes[:3]:  # 最初の3つだけ表示
                logger.info(f"   {proc}")
        else:
            logger.warning("🔍 VOICEVOXプロセスが見つかりません")
    except Exception as e:
        logger.error(f"🔍 プロセス確認エラー: {e}")
    
    try:
        # ポート確認
        netstat_result = subprocess.run(['netstat', '-tlnp'], capture_output=True, text=True, timeout=5)
        port_50021 = [line for line in netstat_result.stdout.split('\n') if '50021' in line]
        if port_50021:
            logger.info(f"🔍 ポート50021の状態:")
            for port_line in port_50021:
                logger.info(f"   {port_line}")
        else:
            logger.warning("🔍 ポート50021がリッスンしていません")
    except Exception as e:
        logger.error(f"🔍 ポート確認エラー: {e}")

def find_working_voicevox_url():
    """強化されたVOICEVOX URL検索"""
    logger.info("🚀 VOICEVOX URL検索開始")
    
    # システム診断実行
    check_system_processes()
    
    urls_to_test = []
    if VOICEVOX_URL_FROM_ENV:
        urls_to_test.insert(0, VOICEVOX_URL_FROM_ENV)
        logger.info(f"🔧 環境変数URL: {VOICEVOX_URL_FROM_ENV}")
    urls_to_test.extend([url for url in VOICEVOX_URLS if url not in urls_to_test])
    
    logger.info(f"🧪 テスト対象URL: {len(urls_to_test)}個")
    
    for i, url in enumerate(urls_to_test, 1):
        logger.info(f"📡 ({i}/{len(urls_to_test)}) テスト開始: {url}")
        
        try:
            # Step 1: バージョンチェック（詳細ログ）
            logger.info(f"   ステップ1: GET {url}/version")
            version_response = requests.get(f"{url}/version", timeout=5)
            logger.info(f"   レスポンス: {version_response.status_code}")
            
            if version_response.status_code == 200:
                version_info = version_response.json()
                version = version_info.get('version', 'unknown')
                logger.info(f"   ✅ バージョン: v{version}")
                
                # Step 2: スピーカーリストチェック
                logger.info(f"   ステップ2: GET {url}/speakers")
                speakers_response = requests.get(f"{url}/speakers", timeout=5)
                logger.info(f"   レスポンス: {speakers_response.status_code}")
                
                if speakers_response.status_code == 200:
                    speakers = speakers_response.json()
                    logger.info(f"   ✅ スピーカー: {len(speakers)}個")
                    
                    # Step 3: 簡単な音声合成テスト
                    logger.info(f"   ステップ3: POST {url}/audio_query")
                    test_query = requests.post(
                        f"{url}/audio_query",
                        params={'text': 'テスト', 'speaker': 3},
                        timeout=8
                    )
                    logger.info(f"   レスポンス: {test_query.status_code}")
                    
                    if test_query.status_code == 200:
                        logger.info(f"   ✅ 音声クエリテスト成功")
                        logger.info(f"🎯 VOICEVOX URL決定: {url}")
                        return url
                    else:
                        logger.warning(f"   ❌ 音声クエリテスト失敗: {test_query.status_code}")
                else:
                    logger.warning(f"   ❌ スピーカーリスト取得失敗: {speakers_response.status_code}")
            else:
                logger.warning(f"   ❌ バージョンチェック失敗: {version_response.status_code}")
                
        except requests.exceptions.Timeout as e:
            logger.warning(f"   ⏰ タイムアウト: {e}")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"   🔌 接続エラー: {e}")
        except Exception as e:
            logger.error(f"   ❌ 予期しないエラー: {e}")
    
    logger.error("❌ 利用可能なVOICEVOX URLが見つかりません")
    logger.info("🔧 トラブルシューティング情報:")
    logger.info("   1. VOICEVOXエンジンが起動しているか確認")
    logger.info("   2. ポート50021がリッスンしているか確認")
    logger.info("   3. Docker内でのネットワーク設定を確認")
    
    return None

# VOICEVOX接続テスト実行
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
    test_response = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": "Hi"}], 
        model="llama3-8b-8192", 
        max_tokens=3
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

class UserDataContainer:
    def __init__(self, user_uuid, user_name, interaction_count):
        self.user_uuid = user_uuid
        self.user_name = user_name
        self.interaction_count = interaction_count

def get_or_create_user(user_uuid, user_name):
    """ユーザーデータ取得"""
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
    """AI応答生成"""
    if not groq_client:
        return f"{user_data.user_name}さん、こんにちは！"
    
    system_prompt = f"あなたは「もちこ」です。{user_data.user_name}さんと話します。40文字以内で親しみやすく返事してください。一人称「あてぃし」"
    
    try:
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt}, 
                {"role": "user", "content": message or "こんにちは"}
            ], 
            model="llama3-8b-8192",
            temperature=0.8,
            max_tokens=80
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI応答エラー: {e}")
        return f"{user_data.user_name}さん、ちょっと考え中です！"

# --- 音声キャッシュ機能 ---
def get_cache_key(text, speaker_id):
    return f"{hash(text)}_{speaker_id}"

def get_cached_voice(text, speaker_id):
    with cache_lock:
        key = get_cache_key(text, speaker_id)
        return voice_cache.get(key)

def cache_voice(text, speaker_id, voice_data):
    with cache_lock:
        if len(voice_cache) >= CACHE_MAX_SIZE:
            oldest_key = next(iter(voice_cache))
            del voice_cache[oldest_key]
        key = get_cache_key(text, speaker_id)
        voice_cache[key] = voice_data

# --- 強化された音声生成 ---
def generate_voice_fast(text, speaker_id=3):
    """強化された音声生成（詳細ログ付き）"""
    if not WORKING_VOICEVOX_URL:
        logger.warning("🚫 VOICEVOX_URL未設定 - 音声生成スキップ")
        return None
    
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH:
        original_length = len(text)
        text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
        logger.info(f"📝 テキスト短縮: {original_length} → {len(text)}文字")
    
    # キャッシュチェック
    cached_voice = get_cached_voice(text, speaker_id)
    if cached_voice:
        logger.info(f"🚀 キャッシュヒット: '{text[:20]}...' ({len(cached_voice)}bytes)")
        return cached_voice
    
    logger.info(f"🎵 音声合成開始: '{text}' (speaker:{speaker_id})")
    start_time = time.time()
    
    try:
        # Step 1: audio_query
        query_url = f"{WORKING_VOICEVOX_URL}/audio_query"
        query_params = {'text': text, 'speaker': speaker_id}
        logger.info(f"📤 Query送信: {query_url} - params: {query_params}")
        
        query_response = requests.post(
            query_url,
            params=query_params,
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        
        logger.info(f"📥 Query応答: {query_response.status_code}")
        if query_response.status_code != 200:
            logger.error(f"❌ Query失敗: {query_response.status_code}")
            logger.error(f"レスポンス内容: {query_response.text[:200]}")
            return None
            
        query_data = query_response.json()
        logger.info(f"📋 Query成功: データサイズ {len(str(query_data))}文字")
        
        # Step 2: synthesis
        synthesis_url = f"{WORKING_VOICEVOX_URL}/synthesis"
        synthesis_params = {'speaker': speaker_id}
        logger.info(f"📤 Synthesis送信: {synthesis_url} - speaker: {speaker_id}")
        
        synthesis_response = requests.post(
            synthesis_url,
            params=synthesis_params,
            json=query_data,
            timeout=VOICEVOX_FAST_TIMEOUT * 2,
            headers={'Content-Type': 'application/json'}
        )
        
        logger.info(f"📥 Synthesis応答: {synthesis_response.status_code}")
        if synthesis_response.status_code != 200:
            logger.error(f"❌ Synthesis失敗: {synthesis_response.status_code}")
            logger.error(f"レスポンス内容: {synthesis_response.text[:200]}")
            return None
        
        voice_data = synthesis_response.content
        if not voice_data or len(voice_data) < 1000:
            logger.error(f"❌ 無効な音声データ: {len(voice_data) if voice_data else 0}bytes")
            return None
        
        elapsed = time.time() - start_time
        logger.info(f"✅ 音声合成成功: {elapsed:.2f}秒, {len(voice_data)}bytes")
        
        # キャッシュに保存
        cache_voice(text, speaker_id, voice_data)
        logger.info(f"💾 キャッシュ保存完了")
        
        return voice_data
        
    except requests.exceptions.Timeout:
        logger.error(f"⏰ 音声合成タイムアウト({VOICEVOX_FAST_TIMEOUT}s): '{text}'")
        return None
    except requests.exceptions.RequestException as req_error:
        logger.error(f"🌐 リクエストエラー: {req_error}")
        return None
    except Exception as e:
        logger.error(f"❌ 音声合成エラー: {e}")
        return None

# --- エンドポイント ---
executor = ThreadPoolExecutor(max_workers=VOICEVOX_WORKERS)

@app.route('/')
def index():
    return jsonify({
        'service': 'もちこ AI Assistant (デバッグ強化版)',
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
    """強化されたLSLチャットエンドポイント"""
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        
        if not (user_uuid and user_name):
            logger.error("❌ 必須パラメータ不足: uuid or name")
            return "Error: uuid and name required", 400
        
        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message[:30]}...'")
        
        # ユーザーデータ取得
        user_data = get_or_create_user(user_uuid, user_name)
        if not user_data:
            logger.error("❌ ユーザーデータ取得失敗")
            return "Error: User data failed", 500
        
        # AI応答生成
        ai_text = generate_ai_response(user_data, message)
        logger.info(f"🤖 AI応答: '{ai_text}'")
        
        # VOICEVOX利用可能性チェック（詳細ログ）
        if not WORKING_VOICEVOX_URL:
            logger.warning("🚫 VOICEVOX利用不可 - テキストのみ返却")
            logger.info(f"🔧 VOICEVOX_URL: {WORKING_VOICEVOX_URL}")
            logger.info(f"🔧 環境変数URL: {VOICEVOX_URL_FROM_ENV}")
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
                # 音声ファイル保存
                timestamp = int(time.time() * 1000)
                filename = f"voice_{user_uuid[:8]}_{timestamp}.wav"
                filepath = os.path.join('/tmp', filename)
                
                # ディレクトリ確認
                os.makedirs('/tmp', exist_ok=True)
                
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
            logger.warning(f"❌ 音声データ生成失敗: voice_data={'None' if voice_data is None else f'{len(voice_data)}bytes'}")
        
        # 最終レスポンス
        response_text = f"{ai_text}|{audio_url}"
        logger.info(f"📤 最終レスポンス: テキスト='{ai_text}', URL='{audio_url}'")
        
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
    """音声ファイル配信"""
    try:
        filepath = os.path.join('/tmp', filename)
        if not os.path.exists(filepath):
            logger.error(f"❌ 音声ファイル不存在: {filepath}")
            return "File not found", 404
            
        logger.info(f"📁 音声ファイル配信: {filename} ({os.path.getsize(filepath)}bytes)")
        return send_from_directory('/tmp', filename)
    except Exception as e:
        logger.error(f"❌ 音声ファイル配信エラー: {e}")
        return "File not found", 404

@app.route('/health')
def health_check():
    """詳細ヘルスチェック"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'database': 'connected' if DATABASE_URL else 'unavailable',
        'groq_api': 'available' if groq_client else 'unavailable',
        'voicevox': {
            'status': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
            'working_url': WORKING_VOICEVOX_URL,
            'env_url': VOICEVOX_URL_FROM_ENV,
            'test_urls': VOICEVOX_URLS
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
    })

@app.route('/debug/voicevox_retry')
def debug_voicevox_retry():
    """VOICEVOX接続再試行"""
    global WORKING_VOICEVOX_URL
    logger.info("🔄 VOICEVOX URL再検索開始")
    WORKING_VOICEVOX_URL = find_working_voicevox_url()
    return jsonify({
        'retry_result': 'success' if WORKING_VOICEVOX_URL else 'failed',
        'working_url': WORKING_VOICEVOX_URL,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/debug/system_info')
def debug_system_info():
    """システム情報取得"""
    try:
        # プロセス情報
        ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        voicevox_processes = [line for line in ps_result.stdout.split('\n') if 'voicevox' in line.lower()]
        
        # ポート情報
        netstat_result = subprocess.run(['netstat', '-tlnp'], capture_output=True, text=True, timeout=5)
        port_lines = [line for line in netstat_result.stdout.split('\n') if '50021' in line]
        
        return jsonify({
            'processes': {
                'voicevox_count': len(voicevox_processes),
                'voicevox_processes': voicevox_processes[:5]  # 最初の5つ
            },
            'ports': {
                'port_50021': port_lines
            },
            'directories': {
                'tmp_exists': os.path.exists('/tmp'),
                'tmp_writable': os.access('/tmp', os.W_OK),
                'voicevox_engine_exists': os.path.exists('/opt/voicevox_engine')
            }
        })
    except Exception as e:
        return jsonify({'error': str(e)})

# --- アプリケーション実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    
    logger.info("🚀 もちこAI デバッグ強化版起動")
    logger.info(f"📍 {host}:{port}")
    logger.info(f"🗄️ DB: {'OK' if DATABASE_URL else 'NG'}")
    logger.info(f"🤖 Groq: {'OK' if groq_client else 'NG'}")
    logger.info(f"🎵 VOICEVOX: {'OK' if WORKING_VOICEVOX_URL else 'NG'}")
    if WORKING_VOICEVOX_URL:
        logger.info(f"🎯 VOICEVOX URL: {WORKING_VOICEVOX_URL}")
    else:
        logger.error("❌ VOICEVOX利用不可")
        logger.info("🔧 デバッグエンドポイント:")
        logger.info("   /debug/voicevox_retry - 再接続試行")
        logger.info("   /debug/system_info - システム情報")
        logger.info("   /health - 詳細ヘルスチェック")
    
    app.run(host=host, port=port, debug=False, threaded=True)
