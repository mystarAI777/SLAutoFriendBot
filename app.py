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
VOICE_DIR = '/tmp/voices'  # Renderでは/tmpを使用

# 音声ディレクトリを作成し、権限を設定
try:
    os.makedirs(VOICE_DIR, exist_ok=True)
    os.chmod(VOICE_DIR, 0o777)  # 読み書き実行を許可
    logger.info(f"📁 音声ディレクトリを確認/作成しました: {VOICE_DIR}")
except Exception as e:
    logger.error(f"❌ 音声ディレクトリ作成/権限設定エラー: {e}")
    # Renderではファイルシステムの制限があるため、エラーでも続行
    logger.warning("⚠️ 音声ディレクトリの作成に失敗しましたが、続行します")

# ディレクトリがアクセス可能かチェック
if os.path.exists(VOICE_DIR) and not os.access(VOICE_DIR, os.W_OK | os.R_OK):
    logger.warning(f"⚠️ 音声ディレクトリ {VOICE_DIR} にアクセスできません")

# --- 音声キャッシュ設定 ---
voice_cache = {}
CACHE_MAX_SIZE = 100
cache_lock = threading.Lock()

# --- Secret Fileからの設定読み込み ---
def get_secret(name):
    """環境変数または秘密ファイルから設定を取得"""
    # まず環境変数をチェック
    env_value = os.environ.get(name)
    if env_value:
        logger.info(f"環境変数から{name[:4]}***を読み込みました")
        return env_value
    
    # 次に秘密ファイルをチェック
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

# --- Groqクライアントの初期化（エラーハンドリング付き） ---
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

# --- 強化されたVOICEVOX接続テスト ---
VOICEVOX_URLS = [
    'http://localhost:50021',
    'http://127.0.0.1:50021',
    'http://voicevox-engine:50021',
    'http://voicevox:50021'
]

def find_working_voicevox_url(max_retries=3, retry_delay=2):
    """VOICEVOXの動作するURLを検索"""
    logger.info("🚀 VOICEVOX URL検索開始")
    urls_to_test = [url for url in ([VOICEVOX_URL_FROM_ENV] + VOICEVOX_URLS) if url]
    
    for url in urls_to_test:
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"📡 テスト開始: {url} (試行 {attempt}/{max_retries})")
                response = requests.get(f"{url}/version", timeout=5)
                if response.status_code == 200:
                    logger.info(f"🎯 VOICEVOX URL決定: {url}")
                    return url
                logger.warning(f"📡 テスト失敗: {url} - ステータスコード {response.status_code}")
            except requests.exceptions.RequestException as e:
                logger.warning(f"📡 テスト失敗: {url} - エラー: {e}")
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
    
    default_url = 'http://localhost:50021'
    logger.warning(f"❌ 利用可能なVOICEVOX URLが見つかりません。デフォルトURLを使用: {default_url}")
    return default_url

# --- 初期化処理 ---
WORKING_VOICEVOX_URL = find_working_voicevox_url()
logger.info(f"✅ VOICEVOX初期化完了: {WORKING_VOICEVOX_URL}")

# VOICEVOX接続テスト
def test_voicevox_connection():
    """VOICEVOX接続をテスト"""
    if not WORKING_VOICEVOX_URL:
        logger.error("❌ VOICEVOX URLが設定されていません")
        return False
    try:
        response = requests.get(f"{WORKING_VOICEVOX_URL}/speakers", timeout=5)
        response.raise_for_status()
        logger.info(f"✅ VOICEVOX接続テスト成功: {len(response.json())} スピーカー取得")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ VOICEVOX接続テスト失敗: {e}")
        return False

# 音声合成テスト
def test_voice_synthesis():
    """音声合成機能をテスト"""
    if not WORKING_VOICEVOX_URL:
        logger.error("❌ VOICEVOX URLが設定されていません")
        return False
    try:
        test_text = "テスト"
        speaker_id = 3
        logger.info(f"🧪 音声合成テスト開始: テキスト='{test_text}', スピーカー={speaker_id}")
        
        query_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/audio_query",
            params={'text': test_text, 'speaker': speaker_id},
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
        if len(voice_data) > 1000:
            logger.info(f"✅ 音声合成テスト成功: サイズ={len(voice_data)} bytes")
            return True
        else:
            logger.warning("❌ 音声合成テスト失敗: 生成された音声データが小さすぎます")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 音声合成テスト失敗: {e}")
        return False

# VOICEVOXステータスを初期化
VOICEVOX_ENABLED = test_voicevox_connection() and test_voice_synthesis()
if not VOICEVOX_ENABLED:
    logger.warning("⚠️ VOICEVOX接続または音声合成テスト失敗。音声機能が無効化されます。")

# データベース設定の検証
if not DATABASE_URL:
    logger.critical("FATAL: DATABASE_URL が設定されていません")
    sys.exit(1)

if not groq_client:
    logger.critical("FATAL: Groqクライアントの初期化に失敗しました")
    sys.exit(1)

# Flask アプリケーション初期化
app = Flask(__name__)
CORS(app)

# データベース初期化
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
# ★ 強化されたWeb検索機能 - Yahoo!ニュース & Wikipedia対応 ★
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
    """Yahoo!ニュースから最新情報を検索（改良版）"""
    try:
        # クエリを調整 - より具体的な検索を行う
        if "ニュース" in query and len(query) <= 6:
            # 「今日のニュース」のような曖昧な検索の場合
            import datetime
            today = datetime.datetime.now().strftime("%Y年%m月%d日")
            search_query = f"{today} ニュース 速報"
        else:
            search_query = query
            
        search_url = f"https://news.yahoo.co.jp/search?p={quote_plus(search_query)}&ei=UTF-8"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        logger.info(f"📰 Yahoo!ニュース検索: {search_query}")
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # より多様なセレクターで記事を探す
        articles = []
        
        # 検索結果のタイトルと概要を取得
        news_selectors = [
            'div[class*="newsFeed"] a',
            'article a',
            'div[class*="searchResult"] a',
            'h3 a',
            'h2 a',
            '.tit a'
        ]
        
        for selector in news_selectors:
            elements = soup.select(selector)
            for elem in elements[:5]:  # 各セレクターから最大5件
                title = clean_text(elem.get_text())
                if title and len(title) > 15 and title not in articles:  # 重複除外
                    # ニュース記事らしいタイトルかチェック
                    if any(keyword in title for keyword in ['発表', '開始', '決定', '実施', '対策', '問題', '事件', '政府', '企業', '経済', '社会']):
                        articles.append(title)
                        if len(articles) >= 3:
                            break
            if len(articles) >= 3:
                break
        
        if articles:
            result = f"最新ニュース: {' | '.join(articles[:2])}"
            logger.info(f"📰 Yahoo!取得成功: {len(articles)}件")
            return result
        
        return None
        
    except Exception as e:
        logger.error(f"Yahoo!ニュース検索エラー: {e}")
        return None

def search_wikipedia(query):
    """Wikipedia日本語版から情報を検索（改良版）"""
    try:
        # 曖昧な検索語の場合はスキップ
        if query in ["今日のニュース", "ニュース", "最新ニュース"]:
            logger.info("📚 曖昧な検索語のためWikipediaをスキップ")
            return None
            
        # Wikipedia API を使用
        api_url = "https://ja.wikipedia.org/api/rest_v1/page/summary/" + quote_plus(query)
        headers = {
            'User-Agent': 'Mozilla/5.0 (compatible; AI-Assistant/1.0)'
        }

        response = requests.get(api_url, headers=headers, timeout=8)
        
        if response.status_code == 200:
            data = response.json()
            extract = data.get('extract', '')
            title = data.get('title', '')
            
            # ニュース番組や一般的すぎる項目は除外
            if any(exclude in title for exclude in ['ニュース', '今日', 'きょう']) and any(exclude in extract for exclude in ['放送', '番組', 'テレビ']):
                logger.info(f"📚 Wikipedia: ニュース番組情報のためスキップ ({title})")
                return None
                
            if extract and len(extract) > 30:
                # 要約を適切な長さに調整
                summary = extract[:180] + "..." if len(extract) > 180 else extract
                return f"Wikipedia「{title}」: {clean_text(summary)}"
        
        # APIが失敗した場合、検索APIを試す
        search_url = "https://ja.wikipedia.org/api/rest_v1/page/search/" + quote_plus(query)
        search_response = requests.get(search_url, headers=headers, timeout=8)
        
        if search_response.status_code == 200:
            search_data = search_response.json()
            pages = search_data.get('pages', [])
            for page in pages[:3]:  # 上位3件をチェック
                title = page.get('title', '')
                description = page.get('description', '')
                
                # ニュース番組関連を除外
                if any(exclude in title.lower() for exclude in ['ニュース', '今日', 'きょう', '放送', '番組']):
                    continue
                    
                if description and len(description) > 20:
                    return f"Wikipedia「{title}」: {clean_text(description)}"
        
        return None
        
    except Exception as e:
        logger.error(f"Wikipedia検索エラー: {e}")
        return None

def search_google_basic(query):
    """基本的なGoogle検索（スクレイピング）- 改良版"""
    try:
        # クエリを調整
        if "ニュース" in query and len(query) <= 8:
            import datetime
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            search_query = f"{query} {today} 最新"
        else:
            search_query = query
            
        search_url = f"https://www.google.com/search?q={quote_plus(search_query)}&hl=ja&lr=lang_ja&tbm=nws"  # ニュース検索を使用
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        logger.info(f"🔍 Google検索: {search_query}")
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # ニュース検索結果のスニペットを取得
        news_items = []
        
        # ニュース検索結果用のセレクター
        selectors = [
            'div[class*="BNeawe"] span',
            'div[class*="st"]',
            'div[class*="IsZvec"]',
            'div[class*="VwiC3b"]',
            'span[class*="st"]'
        ]
        
        for selector in selectors:
            elements = soup.select(selector)
            for elem in elements:
                text = clean_text(elem.get_text())
                if text and len(text) > 40 and len(text) < 200:
                    # ニュース記事らしい内容かチェック
                    if any(keyword in text for keyword in ['発表', '開始', '決定', '実施', '対策', '企業', '政府', '経済', '社会', '日本', '会社']):
                        news_items.append(text)
                        if len(news_items) >= 2:
                            break
            if len(news_items) >= 2:
                break
        
        if news_items:
            result = f"検索結果: {news_items[0][:100]}..."
            logger.info(f"🔍 Google検索成功: {len(news_items)}件")
            return result
        
        return None
        
    except Exception as e:
        logger.error(f"Google検索エラー: {e}")
        return None

def enhanced_web_search(query):
    """強化されたWeb検索 - 複数ソースから情報を収集（改良版）"""
    logger.info(f"🔍 強化Web検索開始: '{query}'")

    results = []

    # 1. Yahoo!ニュースで最新情報を検索（優先度高）
    yahoo_result = search_yahoo_news(query)
    if yahoo_result:
        results.append(yahoo_result)
        logger.info(f"📰 Yahoo!ニュース結果: {yahoo_result[:100]}...")

    # 2. Google検索で追加情報を検索（Yahoo!で結果がない場合は必須）
    if not results or len(results) < 2:
        google_result = search_google_basic(query)
        if google_result:
            results.append(google_result)
            logger.info(f"🔍 Google検索結果: {google_result[:100]}...")

    # 3. Wikipediaは特定の固有名詞の場合のみ使用
    if query and len(query) > 3 and not any(common in query for common in ['ニュース', '今日', '最新', '速報']):
        wiki_result = search_wikipedia(query)
        if wiki_result:
            results.append(wiki_result)
            logger.info(f"📚 Wikipedia結果: {wiki_result[:100]}...")

    if results:
        # 複数の結果をまとめる
        combined_result = " | ".join(results[:2])  # 最大2件に制限
        logger.info(f"✅ 検索成功: {len(results)}件の情報を取得")
        return combined_result
    else:
        logger.warning(f"❌ 検索失敗: '{query}' の情報が見つかりませんでした")
        return f"「{query}」について調べてみたけど、今は具体的な情報が見つからなかった...ごめんね！"

def should_search(message: str) -> bool:
    """メッセージがWeb検索を必要とするかを判定する（改良版）"""
    # より具体的な検索キーワードパターン
    search_patterns = [
        # 疑問詞
        r'(?:誰|何|どこ|いつ|どう|なぜ|どの).{0,10}(?:ですか|だっけ|？|?)',
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
    """ユーザーデータを取得または作成"""
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
    if not groq_client:
        return "あてぃし、今ちょっと調子悪いかも...またあとで話そ！"

    search_info = ""

    # Web検索が必要かチェック
    if should_search(message):
        logger.info(f"🔍 Web検索を実行します: '{message}'")
        search_info = enhanced_web_search(message)
        logger.info(f"📊 検索結果: {search_info[:200]}...")

    # システムプロンプトを改良
    system_prompt = f"""あなたは「もちこ」という名前の賢いギャルAIです。ユーザーの「{user_data.user_name}」さんと会話します。

もちこのルール：
- 自分のことは「あてぃし」と呼びます。
- 明るく、親しみやすいギャル口調で話します。（例：「まじ？」「てか」「～って感じ」「うける」「ありえん」「～ぢゃん？」）
- 回答は50-80文字程度で、具体的で有用な情報を含めます。
- 以下の「参考情報」がある場合は、その内容の具体的な事実や数字を必ず含めて答えてください。
- 参考情報の内容をそのまま読み上げるのではなく、要点を抜き出して自然な会話に織り込んでください。
- 参考情報がない場合は、「調べてみたけど具体的な情報がなかった」と正直に答えてください。

参考情報（最新のWeb検索結果）：
{search_info if search_info else 'なし'}

重要な指示：
- 参考情報がある場合は、必ずその中の具体的な事実、名前、数字、日付などを使って回答してください
- 曖昧な回答ではなく、検索で得た具体的な情報を含めてください
- 検索結果から得た情報と自分の知識を組み合わせて、有用な回答を作ってください"""

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
    """キャッシュキーを生成"""
    return f"{hash(text)}_{speaker_id}"

def get_cached_voice(text, speaker_id):
    """キャッシュから音声データを取得"""
    with cache_lock:
        return voice_cache.get(get_cache_key(text, speaker_id))

def cache_voice(text, speaker_id, voice_data):
    """音声データをキャッシュに保存"""
    with cache_lock:
        if len(voice_cache) >= CACHE_MAX_SIZE:
            del voice_cache[next(iter(voice_cache))]
        voice_cache[get_cache_key(text, speaker_id)] = voice_data

def generate_voice_fast(text, speaker_id=3):
    """高速音声生成"""
    if not VOICEVOX_ENABLED:
        logger.error("❌ VOICEVOXが無効化されています")
        return None
    
    if not WORKING_VOICEVOX_URL:
        logger.error("❌ VOICEVOX URLが設定されていません")
        return None
    
    if not text or not isinstance(text, str):
        logger.error("❌ 無効なテキスト入力")
        return None
    
    if not isinstance(speaker_id, int) or speaker_id < 0:
        logger.error(f"❌ 無効なスピーカーID: {speaker_id}")
        return None
    
    if len(text) > VOICEVOX_MAX_TEXT_LENGTH:
        text = text[:VOICEVOX_MAX_TEXT_LENGTH]
        logger.info(f"📝 テキストを{VOICEVOX_MAX_TEXT_LENGTH}文字に短縮: {text}")
    
    # キャッシュチェック
    if cached_voice := get_cached_voice(text, speaker_id):
        logger.info(f"✅ キャッシュから音声を取得: {text[:20]}...")
        return cached_voice

    try:
        logger.info(f"🎙️ 音声合成開始: テキスト='{text[:20]}...', スピーカー={speaker_id}")
        
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
        logger.info(f"✅ 音声合成成功: サイズ={len(voice_data)} bytes")
        return voice_data
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ 音声合成リクエストエラー: {e}")
        return None

# 音声ファイル管理
voice_files = {}
voice_files_lock = threading.Lock()

def store_voice_file(filename, voice_data):
    """音声ファイルを保存"""
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
    """バックグラウンドで音声生成"""
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

# Flask ルート定義

@app.route('/')
def index():
    """サービス状態を表示"""
    return jsonify({
        'service': 'もちこ AI Assistant (Enhanced Web Search)',
        'status': 'running',
        'groq_status': 'available' if groq_client else 'unavailable',
        'voicevox_status': 'available' if VOICEVOX_ENABLED else 'unavailable',
        'voicevox_url': WORKING_VOICEVOX_URL,
        'web_search_enabled': 'Yahoo News + Wikipedia + Google',
        'voice_dir': VOICE_DIR,
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/health')
def health():
    """ヘルスチェック用エンドポイント"""
    return jsonify({
        'status': 'healthy',
        'groq': 'ok' if groq_client else 'error',
        'voicevox': 'ok' if VOICEVOX_ENABLED else 'disabled',
        'database': 'ok' if DATABASE_URL else 'error'
    })

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """チャットエンドポイント"""
    try:
        data = request.json or {}
        user_uuid = data.get('uuid')
        user_name = data.get('name')
        message = data.get('message', '')

        if not (user_uuid and user_name):
            return "Error: uuid and name required", 400
        
        logger.info(f"📨 チャット受信: {user_name} ({user_uuid[:8]}...) - '{message}'")
        user_data = get_or_create_user(user_uuid, user_name)
        ai_text = generate_ai_response(user_data, message)
        logger.info(f"🤖 AI応答: '{ai_text}'")
        
        audio_url = ""
        if VOICEVOX_ENABLED:
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
    """音声ファイル配信エンドポイント"""
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
                logger.info(f"🔍 ファイル状態: {status}")
        
        return "Error: Voice file not found", 404

    except Exception as e:
        logger.error(f"❌ 音声配信エラー: {e}", exc_info=True)
        return "Error: Internal server error", 500

# メイン実行部分
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0'
    
    logger.info(f"🚀 Flask アプリケーションを開始します: {host}:{port}")
    logger.info(f"🔧 GROQ API: {'✅ 利用可能' if groq_client else '❌ 利用不可'}")
    logger.info(f"🔧 VOICEVOX: {'✅ 利用可能' if VOICEVOX_ENABLED else '❌ 利用不可'}")
    logger.info(f"🔧 データベース: {'✅ 設定済み' if DATABASE_URL else '❌ 未設定'}")
    logger.info(f"🔧 音声保存先: {VOICE_DIR}")
    
    app.run(host=host, port=port, debug=False)
