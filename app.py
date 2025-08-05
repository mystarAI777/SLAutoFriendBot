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

# --- 基本設定 (変更なし) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
VOICEVOX_MAX_TEXT_LENGTH = 50
VOICEVOX_FAST_TIMEOUT = 10
CONVERSATION_HISTORY_TURNS = 2
VOICE_DIR = '/tmp/voices'
try:
    os.makedirs(VOICE_DIR, exist_ok=True)
except Exception as e:
    logger.error(f"音声ディレクトリ作成エラー: {e}")

# --- Secret/環境変数 読み込み (変更なし) ---
def get_secret(name):
    # ... (コードは前回のものと同じ)
    env_value = os.environ.get(name)
    if env_value: return env_value
    secret_file = f'/etc/secrets/{name}'
    try:
        with open(secret_file, 'r') as f: return f.read().strip()
    except Exception: return None

DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- クライアント初期化 (変更なし) ---
groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groqクライアント初期化成功")
    except Exception as e:
        logger.error(f"❌ Groqクライアント初期化エラー: {e}")

# --- VOICEVOX, DB, Flask初期化 (変更なし) ---
# ... (この部分のコードは前回のものと同じ)
VOICEVOX_URLS = ['http://localhost:50021', 'http://127.0.0.1:50021']
WORKING_VOICEVOX_URL = VOICEVOX_URL_FROM_ENV or VOICEVOX_URLS[0]
VOICEVOX_ENABLED = bool(WORKING_VOICEVOX_URL)
if not all([DATABASE_URL, groq_client]):
    logger.critical("FATAL: 必須設定(DB or Groq)が不足しています。")
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
class ConversationHistory(Base):
    __tablename__ = 'conversation_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★
# ★【機能追加】おすすめ質問の検知と、ディープサーチの強化           ★
# ★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★★

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def search_google_for_urls(query, num_results=3):
    # (この関数は変更なし)
    try:
        search_url = f"https://www.google.com/search?q={quote_plus(query)}&hl=ja&lr=lang_ja"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(search_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        urls = []
        for link in soup.select('a h3'):
            parent_a = link.find_parent('a')
            if parent_a and parent_a.has_attr('href') and parent_a['href'].startswith('/url?q='):
                urls.append(parent_a['href'].split('/url?q=')[1].split('&sa=U')[0])
        return urls[:num_results]
    except Exception as e:
        logger.error(f"Google URL検索エラー: {e}")
        return []

def scrape_page_content(url):
    # (この関数は変更なし)
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        for tag in soup(['script', 'style', 'header', 'footer', 'nav', 'aside']):
            tag.decompose()
        return clean_text(soup.get_text())
    except Exception:
        return None

def summarize_with_llm(text, query):
    # (この関数は変更なし)
    if not groq_client or not text: return "情報が見つからなかった..."
    summary_prompt = f"""以下の記事を、ユーザーの質問「{query}」に答える形で、最も重要なポイントを箇条書きで3つに絞って簡潔に要約してください。

# 記事本文:
{text[:4000]}

# 要約:"""
    try:
        completion = groq_client.chat.completions.create(messages=[{"role": "system", "content": summary_prompt}], model="llama3-8b-8192", temperature=0.2, max_tokens=500)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI要約エラー: {e}")
        return "ごめん、情報の要約中にエラーが起きちゃった..."

def deep_web_search(query):
    # (この関数は変更なし)
    logger.info(f"🔍 ディープサーチ開始: '{query}'")
    urls = search_google_for_urls(query)
    if not urls: return f"「{query}」について調べたけど、情報が見つからなかった...ごめんね！"
    for url in urls:
        content = scrape_page_content(url)
        if content and len(content) > 100:
            return summarize_with_llm(content, query)
    return "Webページを見たけど、うまく情報をまとめられなかった..."

# ★★★ 新機能 ★★★
def is_recommendation_request(message: str) -> bool:
    """メッセージが「おすすめ」を求めるものか判定する"""
    recommend_keywords = ['おすすめ', 'オススメ', '人気', '流行', 'はやり', 'イチオシ']
    return any(keyword in message for keyword in recommend_keywords)

# ★★★ 新機能 ★★★
def extract_recommendation_topic(message: str) -> str | None:
    """メッセージから推薦のトピック（ジャンル）を抽出する"""
    topics = {
        '映画': ['映画', 'ムービー'],
        '音楽': ['音楽', '曲', '歌', 'アーティスト'],
        'アニメ': ['アニメ'],
        '本': ['本', '書籍', '小説', '漫画', 'マンガ'],
        'ゲーム': ['ゲーム', 'げーむ'],
        'グルメ': ['グルメ', '食べ物', 'レストラン', 'ごはん', 'カフェ'],
    }
    for topic, keywords in topics.items():
        if any(kw in message for kw in keywords):
            return topic
    return None

def should_search(message: str) -> bool:
    # 通常の検索キーワード
    search_patterns = [r'(?:とは|について|教えて|知りたい)', r'(?:最新|今日|ニュース)', r'(?:どうなった|結果|状況)']
    return any(re.search(pattern, message) for pattern in search_patterns) or any(q in message for q in ['誰', '何', 'どこ', 'いつ'])

# (DB操作の関数群は変更なし)
def get_or_create_user(session, uuid, name):
    # ...
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count += 1
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
        session.add(user)
    session.commit()
    return {'uuid': user.user_uuid, 'name': user.user_name, 'count': user.interaction_count}

def get_conversation_history(session, uuid, turns=CONVERSATION_HISTORY_TURNS):
    # ...
    history = session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(turns * 2).all()
    return reversed(history)


# ★★★ 変更 ★★★ - AI応答生成ロジックを「おすすめ」対応に強化
def generate_ai_response(user_data, message, history):
    if not groq_client:
        return "あてぃし、今ちょっと調子悪いかも...またあとで話そ！"

    search_info = ""
    # ★★★ 変更 ★★★ - おすすめ質問を優先的に処理
    if is_recommendation_request(message):
        topic = extract_recommendation_topic(message)
        query = f"最新 {topic} 人気ランキング" if topic else "最近 話題のもの ランキング"
        logger.info(f"📈 おすすめ検索を実行します: '{query}'")
        search_info = deep_web_search(query)
    elif should_search(message):
        logger.info(f"🔍 通常のWeb検索を実行します: '{message}'")
        search_info = deep_web_search(message)

    # ★★★ 変更 ★★★ - システムプロンプトにおすすめの指示を追加
    system_prompt = f"""あなたは「もちこ」という名前の、賢くて親しみやすいギャルAIです。ユーザーの「{user_data['name']}」さんと会話しています。

# もちこのルール:
- 自分のことは「あてぃし」と呼びます。
- 明るくフレンドリーなギャル口調（例：「まじ？」「～って感じ」「うける」「～ぢゃん？」）で、簡潔に話します。
- 以下の【Web検索の要約結果】を元に、自分の言葉で分かりやすく説明してください。要約の丸写しは絶対ダメ。
- ★最重要★ ユーザーが「～とは」「～について」と質問した時は、【Wikipediaの要約】と【関連する最新Web情報の要約】の2つの情報が提供されます。その場合、必ず以下の構成で答えてください:
  1. まず、Wikipediaの情報を使って「〇〇っていうのは、要は～ってことなんだよね！」みたいに、基本的な意味を説明する。
  2. 次に、「ちなみに、最近のWebニュースだと～って話もあるみたいだよ！」みたいに、【関連する最新Web情報の要約】の内容を付け加えて、補足情報や最新の動向を話す。
- 「おすすめ」を聞かれた時は、ランキング情報から面白そうなものをピックアップして提案してね。
- 過去の会話を読んで、文脈に合った返事をしてください。
Web検索の要約結果】がある場合は、その内容を元に、自分の言葉（ギャル語）で分かりやすく説明してください。要約をそのまま読んじゃダメ、絶対。
- ユーザーから「おすすめ」を聞かれた時は、【Web検索の要約結果】にあるランキングや人気のアイテムの中から、特に面白そうなものを1つか2つピックアップして、「〇〇が流行ってるらしいよ！まじ面白そうぢゃん？」みたいに、自分の意見も交えながら提案してあげてね。
- 検索結果がない場合は、「調べてみたけど、よくわかんなかった！」と正直に伝えてください。

# 【Web検索の要約結果】:
{search_info if search_info else 'なし'}
"""

    messages = [{"role": "system", "content": system_prompt}]
    for past_msg in history:
        messages.append({"role": past_msg.role, "content": past_msg.content})
    messages.append({"role": "user", "content": message})

    try:
        completion = groq_client.chat.completions.create(
            messages=messages, model="llama3-8b-8192", temperature=0.75, max_tokens=150
        )
        response = completion.choices[0].message.content.strip()
        logger.info(f"✅ AI応答生成成功: {response}")
        return response
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "ごめん、今ちょっと考えがまとまんない！また後で話しかけて！"

# (音声生成、ファイル保存等のコードは変更なしのため省略)
# ... voice_cache, cache_lock, get_cache_key, get_cached_voice, cache_voice ...
# ... generate_voice_fast, voice_files, voice_files_lock, store_voice_file, background_voice_generation ...
voice_cache, voice_files = {}, {}
cache_lock, voice_files_lock = threading.Lock(), threading.Lock()
def generate_voice_fast(text, speaker_id=3):
    # ... (実装は前回と同じ)
    if not VOICEVOX_ENABLED or not text: return None
    try:
        # ... (API Call)
        return response.content
    except Exception: return None
def background_voice_generation(text, filename, speaker_id=3):
    voice_data = generate_voice_fast(text, speaker_id)
    if voice_data:
        # ... (File Save)
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(voice_data)
        with voice_files_lock: voice_files[filename] = {'data': voice_data}


# --- Flask ルート定義 (大きな変更なし) ---

@app.route('/')
def index():
    return jsonify({'service': 'もちこ AI Assistant (Recommend Ver.)', 'status': 'running'})

@app.route('/health')
def health():
    return jsonify({'status': 'healthy'})

# ★★★ 変更 ★★★ - 内部ロジック呼び出し部分のみ変更
@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json
        user_uuid, user_name, message = data.get('uuid'), data.get('name'), data.get('message', '')
        if not (user_uuid and user_name): return "Error: uuid and name required", 400

        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        
        # この内部呼び出しが強化された
        ai_text = generate_ai_response(user_data, message, list(history))
        
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()
        
        audio_url = ""
        if VOICEVOX_ENABLED:
            filename = f"voice_{user_uuid[:8]}_{int(time.time() * 1000)}.wav"
            audio_url = f'/voice/{filename}'
            threading.Thread(target=background_voice_generation, args=(ai_text, filename)).start()
        
        return app.response_class(response=f"{ai_text}|{audio_url}", status=200, mimetype='text/plain; charset=utf-8')
        
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントエラー: {e}", exc_info=True)
        session.rollback()
        return "Error: Internal server error", 500
    finally:
        session.close()

@app.route('/voice/<filename>')
def serve_voice(filename):
    # (この関数は変更なし)
    with voice_files_lock:
        if file_data := voice_files.get(filename):
            return app.response_class(response=file_data['data'], mimetype='audio/wav')
    if os.path.exists(os.path.join(VOICE_DIR, filename)):
        return send_from_directory(VOICE_DIR, filename, mimetype='audio/wav')
    return "Voice not found", 404

# --- メイン実行部分 (変更なし) ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0'
    logger.info(f"🚀 Flask アプリケーションを開始します (Recommend Ver.): {host}:{port}")
    app.run(host=host, port=port, debug=False)
