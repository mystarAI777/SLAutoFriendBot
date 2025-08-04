import os
import requests
import logging
import sys
import time
import threading
import subprocess
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq
import tempfile
from urllib.parse import quote, unquote

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

# --- Web検索設定 ---
SEARCH_API_KEY = os.environ.get('SEARCH_API_KEY')  # 必要に応じて設定
SEARCH_ENGINE_ID = os.environ.get('SEARCH_ENGINE_ID')  # 必要に応じて設定

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
            version_response = requests.get(f"{url}/version", timeout=5)
            if version_response.status_code != 200: continue
            
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

# --- Web検索機能 ---
def search_web(query, max_results=3):
    """簡易的なWeb検索機能（DuckDuckGo Instant Answer APIを使用）"""
    try:
        # DuckDuckGo Instant Answer API（無料）
        search_url = "https://api.duckduckgo.com/"
        params = {
            'q': query,
            'format': 'json',
            'no_html': '1',
            'skip_disambig': '1'
        }
        
        response = requests.get(search_url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            
            # Abstract（要約情報）を取得
            if data.get('Abstract'):
                return f"検索結果: {data['Abstract'][:200]}..."
            
            # RelatedTopicsから情報を取得
            if data.get('RelatedTopics'):
                topics = []
                for topic in data['RelatedTopics'][:2]:
                    if isinstance(topic, dict) and topic.get('Text'):
                        topics.append(topic['Text'][:100])
                if topics:
                    return f"関連情報: " + " / ".join(topics)
        
        # フォールバック：リアルタイム情報の代わりに一般的な回答
        return f"「{query}」について調べましたが、詳細な情報が見つかりませんでした。"
        
    except Exception as e:
        logger.error(f"Web検索エラー: {e}")
        return ""

def should_search(message):
    """メッセージがWeb検索を必要とするかを判定"""
    search_keywords = [
        "最新", "ニュース", "今日", "現在", "いま", "リアルタイム",
        "天気", "株価", "為替", "スポーツ", "芸能", "政治",
        "調べて", "検索", "情報", "どうなっている", "状況"
    ]
    return any(keyword in message for keyword in search_keywords)

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
        return UserDataContainer(user_memory.user_uuid, user_memory.user_name, user_memory.interaction_count)
    finally: 
        session.close()

def generate_ai_response(user_data, message):
    """Web検索結果を含むAI応答生成"""
    search_info = ""
    
    # Web検索が必要かチェック
    if should_search(message):
        logger.info(f"🔍 Web検索実行: {message}")
        search_info = search_web(message)
        if search_info:
            logger.info(f"📊 検索結果取得: {search_info[:100]}...")
    
    # システムプロンプトに検索結果を含める
    system_prompt = f"""あなたは「もちこ」です。{user_data.user_name}さんと話します。
40文字以内で親しみやすく返事してください。ギャル口調で、もちこは自分のことを「あてぃし」と言います。

{f'最新情報: {search_info}' if search_info else ''}"""

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
        logger.error(f"AI応答生成エラー: {e}")
        return "あてぃし、ちょっと調子悪いかも...またあとで話そ！"

def get_cache_key(text, speaker_id): 
    return f"{hash(text)}_{speaker_id}"

def get_cached_voice(text, speaker_id):
    with cache_lock: 
        return voice_cache.get(get_cache_key(text, speaker_id))

def cache_voice(text, speaker_id, voice_data):
    with cache_lock:
        if len(voice_cache) >= CACHE_MAX_SIZE: 
            del voice_cache[next(iter(voice_cache))]
        voice_cache[get_cache_key(text, speaker_id)] = voice_data

def generate_voice_fast(text, speaker_id=3):
    if not WORKING_VOICEVOX_URL: 
        return None
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH: 
        text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
    if cached_voice := get_cached_voice(text, speaker_id): 
        return cached_voice
    
    try:
        query_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/audio_query", 
            params={'text': text, 'speaker': speaker_id}, 
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        query_response.raise_for_status()
        
        synthesis_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/synthesis", 
            params={'speaker': speaker_id}, 
            json=query_response.json(), 
            timeout=VOICEVOX_FAST_TIMEOUT * 6
        )
        synthesis_response.raise_for_status()
        
        voice_data = synthesis_response.content
        cache_voice(text, speaker_id, voice_data)
        return voice_data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 音声合成リクエストエラー: {e}")
        return None

# --- 音声ファイル管理の改善 ---
voice_files = {}  # メモリ内でファイル情報を管理
voice_files_lock = threading.Lock()

def store_voice_file(filename, voice_data):
    """音声データをメモリとディスクの両方に保存"""
    try:
        # ディスクに保存
        os.makedirs(VOICE_DIR, exist_ok=True)
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(voice_data)
        
        # メモリにも保存（高速アクセス用）
        with voice_files_lock:
            voice_files[filename] = {
                'data': voice_data,
                'created_at': time.time(),
                'filepath': filepath
            }
        
        logger.info(f"✅ 音声ファイル保存成功: {filepath}")
        return True
    except Exception as e:
        logger.error(f"❌ 音声ファイル保存エラー: {e}")
        return False

def background_voice_generation(text, filename, speaker_id=3):
    """バックグラウンドで音声ファイルを生成・保存する関数"""
    logger.info(f"🎤 バックグラウンド音声生成開始: {filename}")
    voice_data = generate_voice_fast(text, speaker_id)
    
    if voice_data and len(voice_data) > 1000:
        if store_voice_file(filename, voice_data):
            logger.info(f"✅ バックグラウンド音声生成完了: {filename}")
        else:
            logger.error(f"❌ バックグラウンド音声保存失敗: {filename}")
    else:
        logger.warning(f"🎤 バックグラウンド音声生成失敗: {filename}")

@app.route('/')
def index():
    """ルートURLにアクセスした際に、サービスのステータスを返す"""
    return jsonify({
        'service': 'もちこ AI Assistant (Live with Web Search)',
        'status': 'running',
        'voicevox_status': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
        'web_search_enabled': True,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """非同期対応チャットエンドポイント（Web検索対応）"""
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        
        if not (user_uuid and user_name): 
            return "Error: uuid and name required", 400
        
        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message}'")
        
        # 1. AI応答生成（Web検索結果を含む）
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
            thread.daemon = True  # デーモンスレッドに設定
            thread.start()
            logger.info(f"🚀 音声生成スレッドを開始しました。URL: {audio_url}")
        
        # 4. テキストと音声URLをすぐにクライアントへ返す
        response_text = f"{ai_text}|{audio_url}"
        logger.info(f"📤 即時レスポンス送信: Text='{ai_text}', URL='{audio_url}'")
        
        return app.response_class(
            response=response_text, 
            status=200, 
            mimetype='text/plain; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントエラー: {e}", exc_info=True)
        return "Error: Internal server error", 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    """音声ファイル配信（改善版）"""
    try:
        # まずメモリから確認
        with voice_files_lock:
            if filename in voice_files:
                voice_info = voice_files[filename]
                logger.info(f"🎵 メモリから音声配信: {filename}")
                
                # メモリから直接配信
                return app.response_class(
                    response=voice_info['data'],
                    status=200,
                    mimetype='audio/wav',
                    headers={
                        'Content-Disposition': f'inline; filename="{filename}"',
                        'Content-Length': str(len(voice_info['data'])),
                        'Cache-Control': 'public, max-age=3600'
                    }
                )
        
        # ディスクから確認
        filepath = os.path.join(VOICE_DIR, filename)
        if os.path.exists(filepath):
            logger.info(f"🎵 ディスクから音声配信: {filename}")
            return send_from_directory(VOICE_DIR, filename, mimetype='audio/wav')
        
        # ファイルが見つからない場合の処理
        logger.warning(f"🔍 音声ファイルが見つかりません: {filename}")
        
        # 利用可能なファイル一覧をログ出力
        try:
            available_files = os.listdir(VOICE_DIR) if os.path.exists(VOICE_DIR) else []
            logger.info(f"📁 利用可能な音声ファイル: {available_files[:5]}")  # 最初の5個だけ
        except:
            pass
        
        return jsonify({'error': 'Voice file not found', 'filename': filename}), 404
        
    except Exception as e:
        logger.error(f"❌ 音声配信エラー ({filename}): {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/health')
def health_check():
    return jsonify({
        'status': 'healthy', 
        'voicevox_url': WORKING_VOICEVOX_URL,
        'web_search_enabled': True,
        'voice_cache_size': len(voice_cache),
        'stored_voice_files': len(voice_files)
    })

@app.route('/debug/voices')
def debug_voices():
    """デバッグ用：音声ファイル一覧表示"""
    try:
        with voice_files_lock:
            memory_files = list(voice_files.keys())
        
        disk_files = []
        if os.path.exists(VOICE_DIR):
            disk_files = os.listdir(VOICE_DIR)
        
        return jsonify({
            'memory_files': memory_files,
            'disk_files': disk_files,
            'voice_dir': VOICE_DIR
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    
    # 音声ディレクトリを作成
    os.makedirs(VOICE_DIR, exist_ok=True)
    logger.info(f"🎵 音声ディレクトリ準備完了: {VOICE_DIR}")
    
    app.run(host=host, port=port, debug=False, threaded=True)
