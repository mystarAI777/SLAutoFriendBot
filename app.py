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
from duckduckgo_search import DDGS # <--- ★ 追加

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

# --- ★★★ ここからWeb検索機能の修正 ★★★ ---
def search_web(query, max_results_per_site=2):
    """
    Yahoo!ニュースとWikipediaから情報を検索し、結果を要約して返す。
    """
    search_results = []
    sites = {
        "Yahoo!ニュース": "news.yahoo.co.jp",
        "Wikipedia": "ja.wikipedia.org"
    }

    try:
        with DDGS(timeout=10) as ddgs:
            for site_name, site_url in sites.items():
                search_query = f"{query} site:{site_url}"
                logger.info(f"🔍 {site_name} を検索中: '{search_query}'")
                
                # duckduckgo_searchはジェネレータを返すことがあるのでリストに変換
                results = list(ddgs.text(search_query, max_results=max_results_per_site))
                
                if results:
                    # 結果の本文（body）を結合して要約を作成
                    summary = " / ".join([r.get('body', '') for r in results])
                    search_results.append(f"【{site_name}】{summary[:250]}...") # 長すぎる場合は250文字でカット
    
    except Exception as e:
        logger.error(f"Web検索中にエラーが発生しました: {e}", exc_info=True)
        return f"「{query}」の検索中にエラーが発生しました。"

    if not search_results:
        return f"「{query}」について、Yahoo!ニュースやWikipediaでは関連情報が見つかりませんでした。"
    
    # 結合して最終的な検索結果文字列を作成
    final_summary = "\n".join(search_results)
    logger.info(f"📊 最終的な検索結果の要約:\n{final_summary}")
    
    return final_summary
# --- ★★★ ここまでWeb検索機能の修正 ★★★ ---


def should_search(message):
    """メッセージがWeb検索を必要とするかを判定"""
    search_keywords = [
        "最新", "ニュース", "今日", "現在", "いま", "リアルタイム",
        "天気", "株価", "為替", "スポーツ", "芸能", "政治",
        "調べて", "検索", "情報", "どうなっている", "状況", "とは", "誰"
    ]
    # キーワードのいずれかを含み、かつ長すぎないメッセージを検索対象とする
    return any(keyword in message for keyword in search_keywords) and len(message) < 50

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
    
    if should_search(message):
        logger.info(f"🔍 Web検索実行: '{message}'")
        search_info = search_web(message) # 修正された検索関数を呼び出す
        if search_info:
            logger.info(f"📊 検索結果取得: {search_info[:150]}...")
    
    system_prompt = f"""あなたは「もちこ」という名前の賢いAIアシスタントです。ユーザーの「{user_data.user_name}」さんと会話しています。
あなたは親しみやすいギャル口調で話します。あなた自身を指すときは「あてぃし」と言います。
返答は常に、簡潔かつ要点をまとめて40文字以内で答えてください。

以下のWeb検索結果を参考にして、質問に答えてください。
---
{f'検索結果: {search_info}' if search_info else '検索結果なし'}
---
"""

    try:
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt}, 
                {"role": "user", "content": message or "こんにちは"}
            ],
            model="llama3-8b-8192", 
            temperature=0.8, 
            max_tokens=100,
            top_p=0.9,
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

voice_files = {}
voice_files_lock = threading.Lock()

def store_voice_file(filename, voice_data):
    try:
        os.makedirs(VOICE_DIR, exist_ok=True)
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(voice_data)
        with voice_files_lock:
            voice_files[filename] = {'data': voice_data, 'created_at': time.time(), 'filepath': filepath}
        logger.info(f"✅ 音声ファイル保存成功: {filepath}")
        return True
    except Exception as e:
        logger.error(f"❌ 音声ファイル保存エラー: {e}")
        return False

def background_voice_generation(text, filename, speaker_id=3):
    logger.info(f"🎤 バックグラウンド音声生成開始: {filename}")
    voice_data = generate_voice_fast(text, speaker_id)
    if voice_data and len(voice_data) > 1000:
        if not store_voice_file(filename, voice_data):
            logger.error(f"❌ バックグラウンド音声保存失敗: {filename}")
    else:
        logger.warning(f"🎤 バックグラウンド音声生成失敗 or データサイズ不足: {filename}")

@app.route('/')
def index():
    return jsonify({
        'service': 'もちこ AI Assistant (Live with Yahoo/Wikipedia Search)',
        'status': 'running',
        'voicevox_status': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
        'web_search_enabled': True,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json or {}
        user_uuid, user_name, message = data.get('uuid'), data.get('name'), data.get('message', '')
        if not (user_uuid and user_name): return "Error: uuid and name required", 400
        
        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message}'")
        
        user_data = get_or_create_user(user_uuid, user_name)
        ai_text = generate_ai_response(user_data, message)
        logger.info(f"🤖 AI応答: '{ai_text}'")
        
        audio_url = ""
        if WORKING_VOICEVOX_URL:
            filename = f"voice_{user_uuid[:8]}_{int(time.time() * 1000)}.wav"
            audio_url = f'/voice/{filename}'
            thread = threading.Thread(target=background_voice_generation, args=(ai_text, filename))
            thread.daemon = True
            thread.start()
            logger.info(f"🚀 音声生成スレッドを開始しました。URL: {audio_url}")
        
        response_text = f"{ai_text}|{audio_url}"
        logger.info(f"📤 即時レスポンス送信: Text='{ai_text}', URL='{audio_url}'")
        
        return app.response_class(response=response_text, status=200, mimetype='text/plain; charset=utf-8')
        
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントエラー: {e}", exc_info=True)
        return "Error: Internal server error", 500

@app.route('/voice/<filename>')
def serve_voice(filename):
    try:
        with voice_files_lock:
            if filename in voice_files:
                voice_info = voice_files[filename]
                logger.info(f"🎵 メモリから音声配信: {filename}")
                return send_file(
                    voice_info['filepath'],
                    mimetype='audio/wav',
                    as_attachment=False,
                    download_name=filename
                )
        
        filepath = os.path.join(VOICE_DIR, filename)
        if os.path.exists(filepath):
            logger.info(f"🎵 ディスクから音声配信: {filename}")
            return send_from_directory(VOICE_DIR, filename, mimetype='audio/wav')
        
        logger.warning(f"🔍 音声ファイルが見つかりません: {filename}")
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
    try:
        with voice_files_lock:
            memory_files = list(voice_files.keys())
        disk_files = os.listdir(VOICE_DIR) if os.path.exists(VOICE_DIR) else []
        return jsonify({'memory_files': memory_files, 'disk_files': disk_files, 'voice_dir': VOICE_DIR})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    os.makedirs(VOICE_DIR, exist_ok=True)
    logger.info(f"🎵 音声ディレクトリ準備完了: {VOICE_DIR}")
    app.run(host=host, port=port, debug=False, threaded=True)
