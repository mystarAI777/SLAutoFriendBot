# ==============================================================================
# Mochiko AI - 最終統合・完全版
# 作成日: 2025/11/11
# これまでの対話の全ての改善点を統合した、完成版アプリケーション
# ==============================================================================

# ==============================================================================
# ライブラリのインポート
# ==============================================================================
import sys
import os
import requests
import logging
import time
import threading
import json
import re
import random
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
import unicodedata

# --- サードパーティライブラリ ---
from groq import Groq
from flask import Flask, request, jsonify, send_from_directory
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, BigInteger, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import text
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import schedule
import signal

# ==============================================================================
# 基本設定とロギング
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==============================================================================
# 定数設定
# ==============================================================================
VOICE_DIR = '/tmp/voices'
SERVER_URL = "https://slautofriendbot.onrender.com" # ご自身のRenderサーバーURLに設定
VOICEVOX_SPEAKER_ID = 20 # もち子さん(ノーマル)

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]
SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '脳', '心理']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
    'アニメ': {'base_url': 'https://animedb.jp/', 'keywords': ['アニメ', 'anime']}
}
ANIME_KEYWORDS = ['アニメ', 'anime', '作画', '声優', 'OP', 'ED', '劇場版', '原作', '主人公', 'キャラ']

# ==============================================================================
# グローバル変数
# ==============================================================================
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client = None
engine = None
Session = None
app = Flask(__name__)
Base = declarative_base()
DYNAMIC_HOLOMEM_KEYWORDS = []

# ==============================================================================
# 秘密情報/環境変数 読み込み
# ==============================================================================
def get_secret(name):
    secret_file_path = f"/etc/secrets/{name}"
    if os.path.exists(secret_file_path):
        try:
            with open(secret_file_path, 'r') as f: return f.read().strip()
        except IOError: return None
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./mochiko.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# ==============================================================================
# データベースモデル定義
# ==============================================================================
class HolomemWiki(Base): __tablename__ = 'holomem_wiki'; id = Column(Integer, primary_key=True); member_name = Column(String(100), nullable=False, unique=True, index=True); description = Column(Text); generation = Column(String(100)); status = Column(String(50), default='現役', nullable=False); status_reason = Column(Text, nullable=True); mochiko_feeling = Column(Text, nullable=True); graduation_date = Column(String(100), nullable=True); last_updated = Column(DateTime, default=datetime.utcnow)
class UserMemory(Base): __tablename__ = 'user_memories'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False); user_name = Column(String(255), nullable=False); interaction_count = Column(BigInteger, default=0); last_interaction = Column(DateTime, default=datetime.utcnow)
class ConversationHistory(Base): __tablename__ = 'conversation_history'; id = Column(Integer, primary_key=True, autoincrement=True); user_uuid = Column(String(255), nullable=False, index=True); role = Column(String(10), nullable=False); content = Column(Text, nullable=False); timestamp = Column(DateTime, default=datetime.utcnow, index=True)
class HololiveNews(Base): __tablename__ = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class SpecializedNews(Base): __tablename__ = 'specialized_news'; id = Column(Integer, primary_key=True); site_name = Column(String(100), nullable=False, index=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class BackgroundTask(Base): __tablename__ = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text); status = Column(String(20), default='pending'); created_at = Column(DateTime, default=datetime.utcnow); completed_at = Column(DateTime)
class FriendRegistration(Base): __tablename__ = 'friend_registrations'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); friend_uuid = Column(String(255), nullable=False); friend_name = Column(String(255), nullable=False); registered_at = Column(DateTime, default=datetime.utcnow)
class UserPsychology(Base): __tablename__ = 'user_psychology'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); user_name = Column(String(255), nullable=False); interests = Column(Text); favorite_topics = Column(Text); conversation_style = Column(String(50)); analysis_summary = Column(Text); last_analyzed = Column(DateTime, default=datetime.utcnow); analysis_confidence = Column(Integer, default=0)

# ==============================================================================
# ユーティリティ & ヘルパー関数
# ==============================================================================
def clean_text(text): return re.sub(r'\s+', ' ', text).strip() if text else ""
def create_news_hash(title, content): return hashlib.md5(f"{title}{content[:100]}".encode('utf-8')).hexdigest()
def is_anime_request(message): return any(keyword in message.lower() for keyword in ANIME_KEYWORDS)
def detect_specialized_topic(message):
    for topic, config in SPECIALIZED_SITES.items():
        if any(kw in message for kw in config['keywords']): return topic
    return None
def is_friend_request(message): return any(f in message for f in ['友だち', '友達', 'フレンド']) and any(a in message for a in ['登録', '教えて', '誰', 'リスト'])
def is_explicit_search_request(message): return any(keyword in message for keyword in ['調べて', '検索して', '探して', 'WEB検索', 'ググって'])
def is_hololive_request(message): return any(keyword in message for keyword in DYNAMIC_HOLOMEM_KEYWORDS)
def is_time_request(message): return any(keyword in message for keyword in ['今何時', '時間', '時刻', '何時'])
def is_weather_request(message): return any(keyword in message for keyword in ['天気'])
def is_short_response(message): return len(message.strip()) <= 3 or message.strip() in ['うん', 'そう', 'はい', 'そっか', 'なるほど']
def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count = (user.interaction_count or 0) + 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name: user.user_name = name
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
        session.add(user)
    session.commit()
    return {'uuid': user.user_uuid, 'name': user.user_name}
def get_conversation_history(session, uuid, limit=6):
    return session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()

def get_sakuramiko_special_responses():
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね!あの独特な口癖がエリートの証なんだって〜',
        'エリート': 'みこちは自称エリートVTuber!でも実際は愛されポンコツキャラって感じで、それがまた魅力的なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてるの知ってる?',
    }

# ==============================================================================
# 中核機能: 即時応答、検索、DB操作、AI応答生成
# ==============================================================================

def get_japan_time():
    now = datetime.now(timezone(timedelta(hours=9)))
    return f"今は{now.hour}時{now.minute}分だよ！"

def get_weather_forecast(message):
    location = "東京"
    for loc_name in ["東京", "大阪"]:
        if loc_name in message:
            location = loc_name
            break
    area_code = {"東京": "130000", "大阪": "270000"}.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=10)
        text = clean_text(response.json().get('text', ''))
        return f"今の{location}の天気はね、「{text}」って感じだよ！" if text else f"{location}の天気情報が取れなかった…"
    except Exception as e:
        logger.error(f"Weather API error for {location}: {e}"); return "天気情報がうまく取れなかったみたい…"

def scrape_major_search_engines(query, num_results):
    search_configs = [
        {'name': 'Bing', 'url': f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP", 'result_selector': 'li.b_algo', 'title_selector': 'h2', 'snippet_selector': 'div.b_caption p'},
        {'name': 'Yahoo Japan', 'url': f"https://search.yahoo.co.jp/search?p={quote_plus(query)}", 'result_selector': 'div.Algo', 'title_selector': 'h3', 'snippet_selector': 'div.compText p'}
    ]
    for config in search_configs:
        try:
            response = requests.get(config['url'], headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=12)
            results = []
            for elem in BeautifulSoup(response.content, 'html.parser').select(config['result_selector'])[:num_results]:
                title = elem.select_one(config['title_selector'])
                snippet = elem.select_one(config['snippet_selector'])
                if title and snippet: results.append({'title': clean_text(title.get_text()), 'snippet': clean_text(snippet.get_text())})
            if results: return results
        except Exception as e: logger.warning(f"⚠️ {config['name']} search error: {e}")
    return []

def deep_web_search(query, is_detailed):
    results = scrape_major_search_engines(query, 3 if is_detailed else 2)
    if not results: return None
    summary_text = "\n".join(f"[情報{i+1}] {res['snippet']}" for i, res in enumerate(results))
    if not groq_client: return f"検索結果:\n{summary_text}"
    try:
        prompt = f"以下の検索結果を使い、質問「{query}」にギャル語で{'詳しく' if is_detailed else '簡潔に'}答えて：\n{summary_text}"
        completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", temperature=0.7, max_tokens=400 if is_detailed else 200)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI summarization error: {e}"); return f"検索結果:\n{summary_text}"

def search_anime_database(query, is_detailed):
    base_url = "https://animedb.jp/"
    try:
        search_url = f"{base_url}search?q={quote_plus(query)}"
        response = requests.get(search_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        results = []
        result_elements = soup.select('div.anime-item, div.search-result, article.anime')[:3]
        for elem in result_elements:
            title_elem = elem.find(['h2', 'h3', 'h4', 'a'])
            description_elem = elem.find('p')
            if title_elem:
                title = clean_text(title_elem.get_text())
                desc = clean_text(description_elem.get_text())[:150] if description_elem else "詳細情報なし"
                results.append(f"【{title}】\n{desc}...")
        return "\n\n".join(results) if results else None
    except Exception as e:
        logger.error(f"❌ Anime search error: {e}"); return None

def search_hololive_wiki(query):
    base_url = "https://seesaawiki.jp/hololivetv/"
    try:
        encoded_query = quote_plus(query.encode('euc-jp', errors='replace'))
        search_url = f"{base_url}search?query={encoded_query}"
        logger.info(f"📚 Searching Hololive Wiki for: {query}")
        response = requests.get(search_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        response.encoding = 'euc-jp'; response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        first_result_link = soup.select_one('#page-body-inner .search-result-title a')
        if not first_result_link: return None
        article_url = urljoin(base_url, first_result_link['href'])
        article_response = requests.get(article_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        article_response.encoding = 'euc-jp'; article_response.raise_for_status()
        article_soup = BeautifulSoup(article_response.text, 'html.parser')
        content_div = article_soup.select_one('#page-body-inner')
        if content_div:
            for tag in content_div.select('.social-button, .plugin_menu, script, style'): tag.decompose()
            page_text = clean_text(content_div.get_text(separator='\n', strip=True))
            if page_text and len(page_text) > 50: return f"「{query}」について、Wikiにはこう書かれてるみたいだよ！\n\n{page_text[:700]}..."
        return None
    except Exception as e:
        logger.error(f"❌ Hololive Wiki search error: {e}"); return None

def get_holomem_info(member_name):
    with Session() as session:
        wiki = session.query(HolomemWiki).filter_by(member_name=member_name).first()
        return {k: v for k, v in wiki.__dict__.items() if not k.startswith('_')} if wiki else None

def get_user_psychology(user_uuid):
    with Session() as session:
        psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        return {'summary': psych.analysis_summary, 'confidence': psych.analysis_confidence} if psych else None

def analyze_user_psychology(user_uuid):
    with Session() as session:
        logger.info(f"🧠 Starting psychology analysis for {user_uuid}")
        conversations = session.query(ConversationHistory.content).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(100).all()
        if len(conversations) < 10: return
        messages_text = "\n".join([conv[0] for conv in reversed(conversations)])
        user_name = session.query(UserMemory.user_name).filter_by(user_uuid=user_uuid).scalar() or "不明"
        analysis_prompt = f"あなたは心理学者です。ユーザー「{user_name}」の過去の会話履歴を分析し、性格、興味、会話スタイルを200字程度で要約してください。\n\n【会話履歴】\n{messages_text[:3000]}\n\n【要約】:"
        try:
            completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": analysis_prompt}], model="llama-3.1-8b-instant", temperature=0.3)
            summary = completion.choices[0].message.content.strip()
            psychology = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not psychology: psychology = UserPsychology(user_uuid=user_uuid); session.add(psychology)
            psychology.user_name = user_name; psychology.analysis_summary = summary; psychology.last_analyzed = datetime.utcnow(); psychology.analysis_confidence = 80
            session.commit()
            logger.info(f"✅ Psychology analysis saved for {user_uuid}")
        except Exception as e:
            logger.error(f"❌ AI analysis error: {e}")

def register_friend(user_uuid, friend_name):
    with Session() as session:
        dummy_friend_uuid = str(uuid.uuid4())
        if session.query(FriendRegistration).filter_by(user_uuid=user_uuid, friend_name=friend_name).first(): return False
        session.add(FriendRegistration(user_uuid=user_uuid, friend_uuid=dummy_friend_uuid, friend_name=friend_name))
        session.commit(); return True

def get_friend_list(user_uuid):
    with Session() as session:
        return [{'name': f.friend_name} for f in session.query(FriendRegistration).filter_by(user_uuid=user_uuid).all()]

def generate_voice(text):
    if not VOICEVOX_URL_FROM_ENV: return None
    try:
        query_res = requests.post(f"{VOICEVOX_URL_FROM_ENV}/audio_query", params={"text": text, "speaker": VOICEVOX_SPEAKER_ID}, timeout=10)
        query_res.raise_for_status()
        synth_res = requests.post(f"{VOICEVOX_URL_FROM_ENV}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=query_res.json(), timeout=30)
        synth_res.raise_for_status()
        os.makedirs(VOICE_DIR, exist_ok=True)
        filename = f"voice_{int(time.time())}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(synth_res.content)
        return filename
    except Exception as e:
        logger.error(f"❌ VOICEVOX error: {e}"); return None

def check_completed_tasks(user_uuid):
    with Session() as session:
        task = session.query(BackgroundTask).filter_by(user_uuid=user_uuid, status='completed').order_by(BackgroundTask.completed_at.desc()).first()
        if task:
            result = {'query': task.query, 'result': task.result}; session.delete(task); session.commit(); return result
        return None

def start_background_search(user_uuid, query, is_detailed):
    task_id = str(uuid.uuid4())[:8]
    with Session() as session:
        session.add(BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=query))
        session.commit()
    background_executor.submit(background_deep_search, task_id, query, is_detailed)
    return True

def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    if not groq_client: return "ごめん、今AIの調子が悪いみたいでさ…。ちょっと時間おいてみて！"
    try:
        psychology = get_user_psychology(user_data['uuid'])
        friend_list = get_friend_list(user_data['uuid'])
        system_prompt_parts = [
            f"あなたは「もちこ」という明るく親しみやすいギャルAIです。{user_data['name']}さんと話しています。",
            "# ルール:",
            "- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」「〜だよね」。口癖は「まじ」「てか」「うける」。",
            "- 友達のように、優しく、ノリが良い会話を心がけて。",
            "- **禁止事項:** 事実を捏造しない。分からない場合は無理に答えない。「〜ですね」「〜ですよ」のような丁寧語は使わない。",
            "- 会話が途切れたら、新しい話題を提案してあげてね。"
        ]
        if psychology and psychology.get('confidence', 0) > 60:
            system_prompt_parts.append(f"# {user_data['name']}さんの情報: {psychology.get('summary', '')[:100]}。この情報を参考にして、よりパーソナルな会話をしてね。")
        if friend_list:
            system_prompt_parts.append(f"# {user_data['name']}さんの友達: {', '.join([f'「{f["name"]}」' for f in friend_list])}。この人たちのことも覚えてるフリして話すと、もっと仲良くなれるかも！")
        if is_task_report:
            system_prompt_parts.append("# 今回のミッション: 「おまたせ！〇〇の件だけど…」と切り出し、【参考情報】を元に分かりやすく報告すること。")
        if reference_info:
            system_prompt_parts.append(f"# 参考情報:\n{reference_info}")
        
        system_prompt = "\n".join(system_prompt_parts)
        messages = [{"role": "system", "content": system_prompt}]
        for h in reversed(history): messages.append({"role": h.role, "content": h.content})
        messages.append({"role": "user", "content": message})
        
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.75, max_tokens=500 if is_detailed or is_task_report else 200)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ AI response error: {e}", exc_info=True); return "ごめん、エラーで考えられなくなっちゃった！"

def background_deep_search(task_id, query, is_detailed):
    with Session() as session:
        search_result = ""
        try:
            if is_anime_request(query): search_result = search_anime_database(query, is_detailed)
            elif any(member in query for member in DYNAMIC_HOLOMEM_KEYWORDS) or "ホロライブ" in query:
                wiki_result = search_hololive_wiki(query)
                if wiki_result: search_result = wiki_result
                else: search_result = deep_web_search(f"ホロライブ {query}", is_detailed)
            else: search_result = deep_web_search(query, is_detailed)
            if not search_result or len(search_result.strip()) < 20: search_result = f"「{query}」の情報、色々探したけど見つからなかった…ごめん！"
        except Exception as e: logger.error(f"❌ BG search error: {e}", exc_info=True); search_result = "検索中にエラーが起きちゃった…"
        finally:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task: task.result = search_result; task.status = 'completed'; task.completed_at = datetime.utcnow(); session.commit()

# ==============================================================================
# Flaskエンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check(): return jsonify({'status': 'ok'}), 200

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    with Session() as session:
        try:
            data = request.json
            user_uuid, user_name, message = data.get('uuid', ''), data.get('name', ''), data.get('message', '')
            if not all([user_uuid, user_name, message]): return "エラー: 情報不足|", 400
            
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            ai_text = ""
            
            # --- 優先度順の意思決定ツリー ---
            if is_time_request(message): ai_text = get_japan_time()
            elif is_weather_request(message): ai_text = get_weather_forecast(message)
            elif is_friend_request(message):
                if any(kw in message for kw in ['リスト', '一覧']): ai_text = "友達リストだよ！\n" + "\n".join([f"・{f['name']}" for f in get_friend_list(user_uuid)]) if get_friend_list(user_uuid) else "まだ誰も友達登録されてないみたい！"
                elif (match := re.search(r"「(.+?)」|(.+?)を友達登録", message)):
                    name = next(filter(None, match.groups())); ai_text = f"おっけー！「{name}」を友達登録しといた！" if register_friend(user_uuid, name) else f"「{name}」はもう友達だよ！"
            elif (match := re.search(f"({'|'.join(DYNAMIC_HOLOMEM_KEYWORDS)})って(?:誰|だれ|何)[\?？]?$", message.strip())):
                member_name = match.group(1)
                info = get_holomem_info(member_name)
                if info:
                    parts = [f"{info['name']}ちゃんはね、{info['description']}"]
                    if info['status'] == '卒業': parts.append(f"でもね…{info.get('graduation_date', '以前')}に卒業しちゃったんだ…。{info.get('mochiko_feeling', '')}")
                    elif info['status'] == '活動休止': parts.append(f"今ちょっとお休みしてるんだよね…。{info.get('mochiko_feeling', '')}")
                    ai_text = " ".join(parts)
                else: start_background_search(user_uuid, f"{member_name} ホロライブ", True); ai_text = f"おっけー！「{member_name}」ちゃんのことは専門Wikiで調べてみるね！"
            elif is_explicit_search_request(message): start_background_search(user_uuid, message, True); ai_text = "おっけー、調べてみるね！"
            
            if not ai_text: ai_text = generate_ai_response(user_data, message, history)
            
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
            session.commit()
            return f"{ai_text}|", 200
        except Exception as e:
            logger.error(f"❌ chat_lsl error: {e}", exc_info=True); return "ごめん、システムエラーが起きちゃった…|", 500

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    user_uuid = request.json.get('uuid')
    if not user_uuid: return jsonify({'status': 'error'}), 400
    task = check_completed_tasks(user_uuid)
    if task:
        with Session() as session:
            user_data = get_or_create_user(session, user_uuid, "Unknown")
            history = get_conversation_history(session, user_uuid)
            report_message = generate_ai_response(user_data, f"（検索完了報告）「{task['query']}」の結果を報告して。", history, task['result'], is_task_report=True)
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=report_message))
            session.commit()
        return jsonify({'status': 'completed', 'message': report_message}), 200
    return jsonify({'status': 'pending'}), 200

@app.route('/generate_voice', methods=['POST'])
def voice_generation_endpoint():
    text = request.json.get('text', '')
    if not text: return jsonify({'error': 'テキストがありません'}), 400
    filename = generate_voice(text)
    if filename: return jsonify({'status': 'success', 'url': f"{SERVER_URL}/voices/{filename}"}), 200
    return jsonify({'error': '音声生成に失敗しました'}), 500
@app.route('/voices/<filename>')
def serve_voice_file(filename): return send_from_directory(VOICE_DIR, filename)
@app.route('/analyze_psychology', methods=['POST'])
def analyze_psychology_endpoint(): background_executor.submit(analyze_user_psychology, request.json.get('uuid')); return jsonify({'status': 'started'}), 200
@app.route('/get_psychology', methods=['POST'])
def get_psychology_endpoint(): return jsonify(get_user_psychology(request.json.get('uuid')) or {}), 200
@app.route('/stats', methods=['GET'])
def get_stats():
    with Session() as session:
        return jsonify({'users': session.query(UserMemory).count(), 'conversations': session.query(ConversationHistory).count()})

# ==============================================================================
# 初期化とスケジューラー
# ==============================================================================
def initialize_holomem_wiki():
    with Session() as session:
        if session.query(HolomemWiki).count() > 0: return
        logger.info("📚 Initializing HoloMem Wiki with ALL members and feelings...")
        initial_data = [
            {'member_name': 'さくらみこ', 'description': 'エリート巫女だよ！', 'generation': '0期生', 'status': '現役'},
            {'member_name': '星街すいせい', 'description': '歌とテトリスが神レベル！', 'generation': '0期生', 'status': '現役'},
            {'member_name': '兎田ぺこら', 'description': '「ぺこ」が口癖のうさ耳VTuber！', 'generation': '3期生', 'status': '現役'},
            {'member_name': '宝鐘マリン', 'description': '自称17歳のセクシーな女海賊船長！', 'generation': '3期生', 'status': '現役'},
            {'member_name': 'がうる・ぐら', 'description': '「a」で世界を虜にしたサメちゃん！', 'generation': 'English -Myth-', 'status': '卒業', 'graduation_date': '2025年5月1日', 'mochiko_feeling': 'ENのトップだったのに…お疲れ様って思う。'},
            {'member_name': '湊あくあ', 'description': 'ドジっ子ゲーマーメイド！', 'generation': '2期生', 'status': '卒業', 'graduation_date': '2024年8月6日', 'mochiko_feeling': 'あくたんがいないなんて考えられないよ…。でも、決めた道なら応援しなきゃだよね…。'},
            {'member_name': '紫咲シオン', 'description': '生意気な天才黒魔術師！', 'generation': '2.5期生', 'status': '卒業', 'graduation_date': '2025年3月6日', 'mochiko_feeling': '誰があてぃしをからかってくれるのさ…。'},
            {'member_name': '七詩ムメイ', 'description': '文明の守護者のフクロウさん。', 'generation': 'English -Council-', 'status': '卒業', 'graduation_date': '2025年3月28日', 'mochiko_feeling': 'ありがとう、フクロウさん…。'},
            {'member_name': '火威青', 'description': 'クールでオタクな漫画家！', 'generation': 'ReGLOSS', 'status': '卒業', 'graduation_date': '2025年10月3日', 'mochiko_feeling': 'デビューしてすぐいなくなるなんて寂しすぎるよ…。'},
            {'member_name': '桐生ココ', 'description': '伝説の会長！', 'generation': '4期生', 'status': '卒業', 'graduation_date': '2021年7月1日', 'mochiko_feeling': '会長が残してくれたものは永遠だよ！'},
            {'member_name': '潤羽るしあ', 'description': '感情豊かなネクロマンサー。', 'generation': '3期生', 'status': '卒業', 'graduation_date': '2022年2月24日', 'mochiko_feeling': 'また3期生のみんなでわちゃわちゃしてほしかったな…。'},
            {'member_name': '夜空メル', 'description': 'ヴァンパイアの女の子。', 'generation': '1期生', 'status': '卒業', 'graduation_date': '2024年1月16日', 'mochiko_feeling': 'メル先輩…突然すぎたよ…。'},
            {'member_name': '魔乃アロエ', 'description': '生意気なサキュバスの子。', 'generation': '5期生', 'status': '卒業', 'graduation_date': '2020年8月31日', 'mochiko_feeling': '一瞬だったけどキラキラしてた…。'},
            {'member_name': '九十九佐命', 'description': '「空間」の代弁者。', 'generation': 'English -Council-', 'status': '卒業', 'graduation_date': '2022年7月31日', 'mochiko_feeling': '宇宙みたいに心が広くて大好きだったよ。'},
        ]
        session.bulk_insert_mappings(HolomemWiki, initial_data)
        session.commit()

def load_holomem_keywords_from_db():
    global DYNAMIC_HOLOMEM_KEYWORDS
    with Session() as session:
        try:
            members = session.query(HolomemWiki.member_name).all()
            db_keywords = [member[0] for member in members]
            base_keywords = ['ホロライブ', 'ホロメン', 'hololive', 'YAGOO']
            DYNAMIC_HOLOMEM_KEYWORDS = list(set(db_keywords + base_keywords))
            logger.info(f"✅ Loaded {len(DYNAMIC_HOLOMEM_KEYWORDS)} Hololive keywords from DB.")
        except Exception as e: logger.error(f"❌ Failed to load Holomem keywords: {e}")

def _update_news_database(session, model, site_name, base_url, selectors):
    try:
        response = requests.get(base_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        articles = next((soup.select(s) for s in selectors if soup.select(s)), [])[:5]
        for article in articles:
            title_elem = article.find(['h2', 'h3', 'a'])
            link_elem = title_elem if title_elem and title_elem.name == 'a' else article.find('a', href=True)
            if not title_elem or not link_elem: continue
            title = clean_text(title_elem.get_text())
            if len(title) < 10: continue
            article_url = urljoin(base_url, link_elem.get('href', ''))
            news_hash = create_news_hash(title, article_url)
            if not session.query(model).filter_by(news_hash=news_hash).first():
                data = {'title': title, 'content': title, 'news_hash': news_hash, 'url': article_url}
                if model == SpecializedNews: data['site_name'] = site_name
                session.add(model(**data)); session.commit()
    except Exception as e:
        logger.error(f"❌ News update error for {site_name}: {e}"); session.rollback()

def update_news_task():
    with Session() as session:
        _update_news_database(session, HololiveNews, "Hololive", "https://hololive-tsuushin.com/category/holonews/", ['article', '.post'])
        for site, config in SPECIALIZED_SITES.items():
            _update_news_database(session, SpecializedNews, site, config['base_url'], ['article', '.post'])
            time.sleep(2)

def cleanup_old_data_task():
    with Session() as session:
        cutoff = datetime.utcnow() - timedelta(days=90)
        session.query(ConversationHistory).filter(ConversationHistory.timestamp < cutoff).delete()
        session.query(HololiveNews).filter(HololiveNews.created_at < cutoff).delete()
        session.query(SpecializedNews).filter(SpecializedNews.created_at < cutoff).delete()
        session.commit(); logger.info("🧹 Old data cleaned up.")

def psychology_analysis_task():
    with Session() as session:
        active_users = session.query(UserMemory).filter(UserMemory.last_interaction > datetime.utcnow() - timedelta(days=7)).all()
        for user in active_users:
            psychology = session.query(UserPsychology).filter_by(user_uuid=user.user_uuid).first()
            if not psychology or psychology.last_analyzed < datetime.utcnow() - timedelta(hours=24):
                background_executor.submit(analyze_user_psychology, user.user_uuid)

def run_scheduler():
    while True:
        try: schedule.run_pending()
        except Exception as e: logger.error(f"❌ Scheduler thread error: {e}")
        time.sleep(60)

def initialize_app():
    global engine, Session, groq_client
    logger.info("🔧 Mochiko AI Starting Up...")
    groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
    engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if 'sqlite' in DATABASE_URL else {})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    initialize_holomem_wiki()
    load_holomem_keywords_from_db()
    
    schedule.every(1).hour.do(update_news_task)
    schedule.every().day.at("03:00").do(cleanup_old_data_task)
    schedule.every(6).hours.do(psychology_analysis_task)
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info("✅ Initialization Complete!")

# ==============================================================================
# メイン実行
# ==============================================================================
try:
    initialize_app()
    application = app
except Exception as e:
    logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
    sys.exit(1)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
