# program 8.8_enhanced_web_search.txt

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
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.orm import declarative_base, sessionmaker
from groq import Groq
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
    'http://localhost:50021', 'http://127.0.0.1:50021',
    'http://voicevox-engine:50021', 'http://voicevox:50021'
]

def find_working_voicevox_url():
    logger.info("🚀 VOICEVOX URL検索開始")
    urls_to_test = [url for url in ([VOICEVOX_URL_FROM_ENV] + VOICEVOX_URLS) if url]
    for url in urls_to_test:
        try:
            logger.info(f"📡 テスト開始: {url}")
            if requests.get(f"{url}/version", timeout=5).status_code == 200:
                logger.info(f"🎯 VOICEVOX URL決定: {url}")
                return url
        except requests.exceptions.RequestException:
            continue
    logger.error("❌ 利用可能なVOICEVOX URLが見つかりません")
    return None

# --- 初期化処理 ---
WORKING_VOICEVOX_URL = find_working_voicevox_url()
logger.info(f"✅ VOICEVOX初期化完了: {WORKING_VOICEVOX_URL or '失敗'}")

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

# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
# ★ 変更点: 強化されたWeb検索機能 - Yahoo!ニュース & Wikipedia対応 ★
# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★

def clean_text(text):
    """HTMLタグを除去し、テキストをクリーンアップ"""
    if not text:
        return ""
    # HTMLタグを除去
    text = re.sub(r'<[^>]+>', '', text)
    # 複数の空白を1つに
    text = re.sub(r'\s+', ' ', text)
    # 改行を空白に
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text.strip()

def search_yahoo_news(query):
    """Yahoo!ニュースから最新情報を検索"""
    try:
        search_url = f"https://news.yahoo.co.jp/search?p={quote_plus(query)}&ei=UTF-8"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(search_url, headers=headers, timeout=8)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # ニュース記事のタイトルと概要を取得
        articles = []
        news_items = soup.find_all('div', class_=['newsFeed_item', 'sc-cHGsZl']) or soup.find_all('article')
        
        for item in news_items[:3]:  # 最新3件
            title_elem = item.find('a') or item.find(['h1', 'h2', 'h3'])
            if title_elem:
                title = clean_text(title_elem.get_text())
                if title and len(title) > 10:  # 意味のあるタイトルのみ
                    articles.append(title)
        
        if articles:
            return f"Yahoo!ニュース最新情報: {' / '.join(articles[:2])}"  # 最大2件
        
        return None
        
    except Exception as e:
        logger.error(f"Yahoo!ニュース検索エラー: {e}")
        return None

def search_wikipedia(query):
    """Wikipedia日本語版から情報を検索"""
    try:
        # Wikipedia API を使用
        api_url = "https://ja.wikipedia.org/api/rest_v1/page/summary/" + quote_plus(query)
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; AI-Assistant/1.0)'
        }
        
        response = requests.get(api_url, headers=headers, timeout=8)
        
        if response.status_code == 200:
            data = response.json()
            extract = data.get('extract', '')
            if extract and len(extract) > 20:
                # 要約を適切な長さに調整
                summary = extract[:200] + "..." if len(extract) > 200 else extract
                return f"Wikipedia: {clean_text(summary)}"
        
        # APIが失敗した場合、検索APIを試す
        search_url = "https://ja.wikipedia.org/api/rest_v1/page/search/" + quote_plus(query)
        search_response = requests.get(search_url, headers=headers, timeout=8)
        
        if search_response.status_code == 200:
            search_data = search_response.json()
            pages = search_data.get('pages', [])
            if pages:
                # 最初の検索結果の詳細を取得
                first_page = pages[0]
                title = first_page.get('title', '')
                description = first_page.get('description', '')
                if description:
                    return f"Wikipedia「{title}」: {clean_text(description)}"
        
        return None
        
    except Exception as e:
        logger.error(f"Wikipedia検索エラー: {e}")
        return None

def search_google_basic(query):
    """基本的なGoogle検索（スクレイピング）"""
    try:
        search_url = f"https://www.google.com/search?q={quote_plus(query)}&hl=ja&lr=lang_ja"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(search_url, headers=headers, timeout=8)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # スニペット（検索結果の説明）を取得
        snippets = []
        for elem in soup.find_all(['span', 'div'], class_=['st', 'IsZvec', 'VwiC3b']):
            text = clean_text(elem.get_text())
            if text and len(text) > 30 and len(text) < 300:
                snippets.append(text)
                if len(snippets) >= 2:
                    break
        
        if snippets:
            return f"検索結果: {snippets[0][:150]}..."
        
        return None
        
    except Exception as e:
        logger.error(f"Google検索エラー: {e}")
        return None

def enhanced_web_search(query):
    """強化されたWeb検索 - 複数ソースから情報を収集"""
    logger.info(f"🔍 強化Web検索開始: '{query}'")
    
    results = []
    
    # 1. Yahoo!ニュースで最新情報を検索
    yahoo_result = search_yahoo_news(query)
    if yahoo_result:
        results.append(yahoo_result)
        logger.info(f"📰 Yahoo!ニュース結果: {yahoo_result[:100]}...")
    
    # 2. Wikipediaで基本情報を検索
    wiki_result = search_wikipedia(query)
    if wiki_result:
        results.append(wiki_result)
        logger.info(f"📚 Wikipedia結果: {wiki_result[:100]}...")
    
    # 3. 両方失敗した場合のみGoogle検索
    if not results:
        google_result = search_google_basic(query)
        if google_result:
            results.append(google_result)
            logger.info(f"🔍 Google検索結果: {google_result[:100]}...")
    
    if results:
        # 複数の結果をまとめる
        combined_result = " | ".join(results)
        logger.info(f"✅ 検索成功: {len(results)}件の情報を取得")
        return combined_result
    else:
        logger.warning(f"❌ 検索失敗: '{query}' の情報が見つかりませんでした")
        return f"「{query}」について調べたけど、今は情報が見つからなかった...ごめんね！"

def should_search(message: str) -> bool:
    """メッセージがWeb検索を必要とするかを判定する（改良版）"""
    # より具体的な検索キーワードパターン
    search_patterns = [
        # 疑問詞
        r'(?:誰|何|どこ|いつ|どう|なぜ|どの).{0,10}(?:ですか|だっけ|？|\?)',
        # 説明要求
        r'(?:とは|って何|について|教えて|知りたい)',
        # 最新情報
        r'(?:最新|今日|昨日|最近|ニュース|現在)',
        # 具体的な質問
        r'(?:どうなった|結果|状況|株価|天気|為替)',
    ]
    
    # パターンマッチング
    for pattern in search_patterns:
        if re.search(pattern, message, re.IGNORECASE):
            return True
    
    # キーワードベースの判定も残す
    search_keywords = [
        "誰", "何", "どこ", "いつ", "教えて", "知りたい",
        "最新", "ニュース", "今日", "天気", "株価", "為替",
        "について", "とは", "どうなった", "結果"
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
    """【強化版】Web検索結果をプロンプトに含めてAI応答を生成"""
    search_info = ""
    
    # Web検索が必要かチェック
    if should_search(message):
        logger.info(f"🔍 Web検索を実行します: '{message}'")
        search_info = enhanced_web_search(message)
        logger.info(f"📊 検索結果: {search_info[:200]}...")

    # システムプロンプトを改良
    system_prompt = f"""あなたは「もちこ」という名前の賢いギャルAIです。ユーザーの「{user_data.user_name}」さんと会話します。

# もちこのルール
- 自分のことは「あてぃし」と呼びます。
- 明るく、親しみやすいギャル口調で話します。（例：「まじ？」「てか」「～って感じ」「うける」「ありえん」「～ぢゃん？」）
- 回答は常に40文字程度の短くて分かりやすい文章にします。
- 以下の「参考情報」がある場合は、その内容を基に正確に答えてください。
- 参考情報の内容を自然な会話に織り込んで答えてください。
- 参考情報がない、または関係ない場合は、知識の範囲で答えるか「分かんない」と答えてください。

# 参考情報（最新のWeb検索結果）
{search_info if search_info else 'なし'}

# 重要
- 参考情報がある場合は、必ずその内容を使って回答してください
- 検索結果を無視せず、必ず活用してください
"""

    try:
        logger.info(f"🤖 Groqに応答生成をリクエストします。")
        completion = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message or "ねぇねぇ、元気？"}
            ],
            model="llama3-8b-8192",
            temperature=0.7,
            max_tokens=120,
        )
        response = completion.choices[0].message.content.strip()
        
        # 検索情報が活用されているかチェック
        if search_info and search_info not in ["なし", ""] and len(search_info) > 50:
            logger.info(f"✅ 検索情報を含む応答を生成しました")
        
        return response

    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "あてぃし、ちょっと調子悪いかも...またあとで話そ！"

# 以下、音声生成やFlaskルートは元のコードと同じ

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
    if not WORKING_VOICEVOX_URL: return None
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH:
        text = text[:VOICEVOX_MAX_TEXT_LENGTH]
    if cached_voice := get_cached_voice(text, speaker_id):
        return cached_voice
    
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
        os.makedirs(VOICE_DIR, exist_ok=True)
        filepath = os.path.join(VOICE_DIR, filename)
        
        # ファイルを先にディスクに保存
        with open(filepath, 'wb') as f: 
            f.write(voice_data)
        
        # メモリにも保存（高速アクセス用）
        with voice_files_lock:
            voice_files[filename] = {
                'data': voice_data, 
                'created_at': time.time(), 
                'filepath': filepath,
                'status': 'ready'  # ステータスを追加
            }
        
        logger.info(f"✅ 音声ファイル保存成功: {filepath} (サイズ: {len(voice_data)} bytes)")
        return True
    except Exception as e:
        logger.error(f"❌ 音声ファイル保存エラー: {e}", exc_info=True)
        return False

def background_voice_generation(text, filename, speaker_id=3):
    logger.info(f"🎤 バックグラウンド音声生成開始: {filename}")
    
    # 生成中ステータスを先に登録
    with voice_files_lock:
        voice_files[filename] = {
            'data': None, 
            'created_at': time.time(), 
            'filepath': os.path.join(VOICE_DIR, filename),
            'status': 'generating'
        }
    
    try:
        voice_data = generate_voice_fast(text, speaker_id)
        if voice_data and len(voice_data) > 1000:
            if store_voice_file(filename, voice_data):
                logger.info(f"✅ バックグラウンド音声生成成功: {filename}")
            else:
                logger.error(f"❌ バックグラウンド音声保存失敗: {filename}")
                # 失敗した場合はステータスを更新
                with voice_files_lock:
                    if filename in voice_files:
                        voice_files[filename]['status'] = 'failed'
        else:
            logger.warning(f"🎤 バックグラウンド音声生成失敗: {filename} - データサイズ不正")
            with voice_files_lock:
                if filename in voice_files:
                    voice_files[filename]['status'] = 'failed'
    except Exception as e:
        logger.error(f"❌ バックグラウンド音声生成エラー ({filename}): {e}", exc_info=True)
        with voice_files_lock:
            if filename in voice_files:
                voice_files[filename]['status'] = 'failed'

@app.route('/')
def index():
    return jsonify({
        'service': 'もちこ AI Assistant (Enhanced Web Search)',
        'status': 'running',
        'voicevox_status': 'available' if WORKING_VOICEVOX_URL else 'unavailable',
        'voicevox_url': WORKING_VOICEVOX_URL,
        'web_search_enabled': 'Yahoo News + Wikipedia + Google',
        'voice_dir': VOICE_DIR,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')
        
        if not (user_uuid and user_name): return "Error: uuid and name required", 400
        
        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message}'")
        user_data = get_or_create_user(user_uuid, user_name)
        ai_text = generate_ai_response(user_data, message)
        logger.info(f"🤖 AI応答: '{ai_text}'")
        
        audio_url = ""
        if WORKING_VOICEVOX_URL:
            timestamp = int(time.time() * 1000)
            filename = f"voice_{user_uuid[:8]}_{timestamp}.wav"
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
        logger.info(f"🎵 音声ファイル要求: {filename}")
        
        # メモリキャッシュをまずチェック
        with voice_files_lock:
            if filename in voice_files:
                voice_info = voice_files[filename]
                status = voice_info.get('status', 'unknown')
                logger.info(f"🎵 メモリ内ファイル状態: {status}")
                
                # 生成完了している場合
                if status == 'ready' and voice_info.get('data'):
                    logger.info(f"🎵 メモリから音声配信成功: {filename}")
                    return app.response_class(
                        response=voice_info['data'], 
                        status=200, 
                        mimetype='audio/wav',
                        headers={
                            'Content-Disposition': f'inline; filename="{filename}"',
                            'Content-Length': str(len(voice_info['data'])),
                            'Cache-Control': 'no-cache'
                        }
                    )
                
                # 生成中の場合は少し待ってからディスクをチェック
                elif status == 'generating':
                    logger.info(f"🎵 音声生成中、ディスクをチェック: {filename}")
                    time.sleep(1)  # 1秒待機
        
        # ディスクファイルをチェック
        filepath = os.path.join(VOICE_DIR, filename)
        if os.path.exists(filepath):
            try:
                # ファイルサイズをチェック
                file_size = os.path.getsize(filepath)
                if file_size > 1000:  # 1KB以上なら有効とみなす
                    logger.info(f"🎵 ディスクから音声配信成功: {filename} ({file_size} bytes)")
                    return send_from_directory(
                        VOICE_DIR, 
                        filename, 
                        mimetype='audio/wav',
                        as_attachment=False
                    )
                else:
                    logger.warning(f"🎵 ファイルサイズが小さすぎます: {filename} ({file_size} bytes)")
            except Exception as e:
                logger.error(f"❌ ディスクファイル読み込みエラー: {e}")
        
        # ファイルが見つからない場合の詳細ログ
        logger.warning(f"🔍 音声ファイルが見つかりません: {filename}")
        logger.info(f"🔍 探索パス: {filepath}")
        logger.info(f"🔍 ディレクトリ存在: {os.path.exists(VOICE_DIR)}")
        
        if os.path.exists(VOICE_DIR):
            files_in_dir = os.listdir(VOICE_DIR)
            logger.info(f"🔍 ディレクトリ内ファイル数: {len(files_in_dir)}")
            if files_in_dir:
                logger.info(f"🔍 最新ファイル例: {files_in_dir[-1] if files_in_dir else 'なし'}")
        
        # メモリ内ファイル状況も表示
        with voice_files_lock:
            logger.info(f"🔍 メモリ内ファイル数: {len(voice_files)}")
            if filename in voice_files:
                status = voice_files[filename].get('status', 'unknown')
                logger.info(f"🔍 要求ファイルステータス: {status}")
        
        return jsonify({
            'error': 'Voice file not found or not ready', 
            'filename': filename,
            'voice_dir': VOICE_DIR,
            'disk_exists': os.path.exists(filepath),
            'memory_status': voice_files.get(filename, {}).get('status', 'not_found')
        }), 404
        
    except Exception as e:
        logger.error(f"❌ 音声配信エラー ({filename}): {e}", exc_info=True)
        return jsonify({'error': 'Internal server error', 'details': str(e)}), 500

@app.route('/health')
def health_check():
    return jsonify({
        'status': 'healthy', 
        'voicevox_url': WORKING_VOICEVOX_URL, 
        'web_search_enabled': 'Yahoo News + Wikipedia + Google', 
        'voice_cache_size': len(voice_cache), 
        'stored_voice_files': len(voice_files)
    })

@app.route('/debug/voices')
def debug_voices():
    try:
        with voice_files_lock: memory_files = list(voice_files.keys())
        disk_files = os.listdir(VOICE_DIR) if os.path.exists(VOICE_DIR) else []
        return jsonify({'memory_files': memory_files, 'disk_files': disk_files, 'voice_dir': VOICE_DIR})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 新しいデバッグエンドポイント
@app.route('/debug/search/<query>')
def debug_search(query):
    """Web検索のテスト"""
    try:
        result = enhanced_web_search(query)
        return jsonify({
            'query': query,
            'result': result,
            'should_search': should_search(query),
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# 音声生成テストエンドポイント
@app.route('/debug/voice_test/<text>')
def debug_voice_test(text):
    """音声生成のテスト"""
    try:
        if not WORKING_VOICEVOX_URL:
            return jsonify({'error': 'VOICEVOX not available'}), 503
        
        timestamp = int(time.time() * 1000)
        filename = f"test_voice_{timestamp}.wav"
        
        # 同期的に音声生成
        voice_data = generate_voice_fast(text[:30], 3)  # 30文字まで
        
        if voice_data and len(voice_data) > 1000:
            if store_voice_file(filename, voice_data):
                return jsonify({
                    'status': 'success',
                    'filename': filename,
                    'url': f'/voice/{filename}',
                    'text': text[:30],
                    'size': len(voice_data),
                    'timestamp': datetime.utcnow().isoformat()
                })
        
        return jsonify({
            'status': 'failed',
            'error': 'Voice generation failed',
            'voice_data_size': len(voice_data) if voice_data else 0
        }), 500
        
    except Exception as e:
        logger.error(f"音声テストエラー: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    os.makedirs(VOICE_DIR, exist_ok=True)
    logger.info(f"🎵 音声ディレクトリ準備完了: {VOICE_DIR}")
    logger.info(f"🔍 強化Web検索機能が有効: Yahoo!ニュース + Wikipedia + Google")
    app.run(host=host, port=port, debug=False, threaded=True)
