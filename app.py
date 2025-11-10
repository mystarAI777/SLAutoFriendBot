import sys
import os
import requests
import logging
import time
import threading
import json
import re
import random
import hashlib
import unicodedata
import traceback 
from datetime import datetime, timedelta, timezone
from groq import Groq
from flask import Response, send_from_directory
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import schedule
import signal
from threading import Lock

# --- 型ヒント ---
try:
    from typing import Union, Dict, Any, List, Optional
except ImportError:
    Dict, Any, List, Union, Optional = dict, object, list, object, object

from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 定数 ---
SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', 'http://localhost:10000')
VOICE_DIR = '/tmp/voices'
VOICEVOX_SPEAKER_ID = 20
SL_SAFE_CHAR_LIMIT = 250
USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36']
LOCATION_CODES = { "東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000" }

SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー', 'blener']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG業界']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '脳', '認知科学']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
    'アニメ': {
        'base_url': 'https://animedb.jp/',
        'keywords': ['アニメ', 'anime', 'ANIME', 'ｱﾆﾒ', 'アニメーション', '作画', '声優', 'OP', 'ED']
    }
}

# --- グローバル変数 & アプリ設定 ---
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client, engine, Session = None, None, None
VOICEVOX_ENABLED = True
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
CORS(app)
Base = declarative_base()

search_context_cache = {}
cache_lock = Lock()
g_holomem_keywords = []

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name):
    path = f"/etc/secrets/{name}"
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            with open(path, 'r') as f: return f.read().strip()
        except IOError: return None
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

if DATABASE_URL and DATABASE_URL.startswith('postgresql'):
    if 'client_encoding' not in DATABASE_URL:
        DATABASE_URL += '?client_encoding=utf8'

# --- データベースモデル ---
class UserMemory(Base): 
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    interaction_count = Column(Integer, default=0)
    last_interaction = Column(DateTime, default=datetime.utcnow)

class ConversationHistory(Base): 
    __tablename__ = 'conversation_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

class UserPsychology(Base):
    __tablename__ = 'user_psychology'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    analysis_summary = Column(Text, nullable=True)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime, nullable=True)
    
class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False, index=True)
    task_type = Column(String(50), nullable=False)
    query = Column(Text, nullable=False)
    result = Column(Text, nullable=True)
    status = Column(String(20), default='pending', index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class HolomemWiki(Base):
    __tablename__ = 'holomem_wiki'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    generation = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True, index=True)
    graduation_date = Column(String(100), nullable=True)
    mochiko_feeling = Column(Text, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow)

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000), unique=True)
    news_hash = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class NewsCache(Base):
    __tablename__ = 'news_cache'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    news_id = Column(Integer, nullable=False)
    news_number = Column(Integer, nullable=False)
    news_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserContext(Base):
    __tablename__ = 'user_context'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    last_context_type = Column(String(50), nullable=False)
    last_query = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# --- ヘルパー関数群 ---
def clean_text(text): return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip() if text else ""
def limit_text_for_sl(text, max_length=SL_SAFE_CHAR_LIMIT): return text[:max_length-3] + "..." if len(text) > max_length else text
def get_japan_time(): return f"今は{datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分')}だよ！"

def is_detailed_request(message): return any(kw in message for kw in ['詳しく', '詳細', 'くわしく', '教えて', '説明して', '解説して', 'どういう', 'なぜ', 'どうして'])
def is_number_selection(message):
    match = re.match(r'^\s*([1-9])', message.strip())
    return int(match.group(1)) if match else None
def format_search_results_as_list(results):
    if not results: return None
    return [{'number': i, 'title': r.get('title', ''), 'snippet': r.get('snippet', ''), 'full_content': r.get('snippet', '')} for i, r in enumerate(results[:5], 1)]

def save_news_cache(session, user_uuid, news_items):
    session.query(NewsCache).filter_by(user_uuid=user_uuid).delete()
    for i, news in enumerate(news_items, 1):
        cache = NewsCache(user_uuid=user_uuid, news_id=news.id, news_number=i, news_type='hololive')
        session.add(cache)
    session.commit()

def get_cached_news_detail(session, user_uuid, news_number):
    cache = session.query(NewsCache).filter_by(user_uuid=user_uuid, news_number=news_number).first()
    if not cache: return None
    return session.query(HololiveNews).filter_by(id=cache.news_id).first()

def save_search_context(user_uuid, search_results, query):
    with cache_lock:
        search_context_cache[user_uuid] = { 'results': search_results, 'query': query, 'timestamp': time.time() }

def get_saved_search_result(user_uuid, number):
    with cache_lock:
        cached_data = search_context_cache.get(user_uuid)
    if cached_data and (time.time() - cached_data['timestamp']) < 600:
        for r in cached_data['results']:
            if r.get('number') == number:
                return r
    return None

def save_user_context(session, user_uuid, context_type, query):
    context = session.query(UserContext).filter_by(user_uuid=user_uuid).first()
    if not context:
        context = UserContext(user_uuid=user_uuid, last_context_type=context_type, last_query=query)
        session.add(context)
    else:
        context.last_context_type = context_type
        context.last_query = query
    session.commit()

def get_user_context(session, user_uuid):
    context = session.query(UserContext).filter_by(user_uuid=user_uuid).first()
    if context and (datetime.utcnow() - context.updated_at).total_seconds() < 600:
        return {'type': context.last_context_type, 'query': context.last_query}
    return None

def is_recommendation_request(message): return any(kw in message for kw in ['おすすめ', 'オススメ', '人気'])
def extract_recommendation_topic(message):
    topics = {'映画': ['映画'], '音楽': ['音楽', '曲'], 'アニメ': ['アニメ'], 'ゲーム': ['ゲーム']}
    return next((topic for topic, keywords in topics.items() if any(kw in message for kw in keywords)), None)
def detect_specialized_topic(message):
    if 'ホロライブ' in message and any(kw in message for kw in ['ニュース', '最新', '情報']): return None
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']): return topic
    return None

def is_time_request(message): return any(kw in message for kw in ['今何時', '時間', '時刻'])
def is_weather_request(message):
    if any(t in message for t in ['天気', '気温']): return next((loc for loc in LOCATION_CODES if loc in message), "東京")
    return None
def is_follow_up_question(message, history):
    if not history: return False
    return any(re.search(p, message) for p in [r'もっと詳しく', r'それについて詳しく', r'なんで？', r'どういうこと'])
def should_search(message):
    if len(message) < 5 or is_number_selection(message): return False
    if is_holomem_name_only_request(message): return False
    if 'ホロライブ' in message and any(kw in message for kw in ['ニュース', '最新', '情報']): return False
    if detect_specialized_topic(message) or is_recommendation_request(message): return True
    if any(re.search(p, message) for p in [r'とは', r'について', r'教えて', r'最新', r'調べて', r'検索', r'ニュース']): return True
    return any(word in message for word in ['誰', '何', 'どこ', 'いつ', 'なぜ', 'どうして'])
def get_or_create_user(session, user_uuid, user_name):
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if not user:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name)
        session.add(user)
    user.interaction_count += 1
    user.last_interaction = datetime.utcnow()
    if user.user_name != user_name: user.user_name = user_name
    session.commit()
    return {'uuid': user.user_uuid, 'name': user.user_name}

def get_conversation_history(session, user_uuid, limit=6):
    return session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()

def get_sakuramiko_special_responses():
    return {
        'にぇ': 'みこちの「にぇ」、まじかわいすぎじゃん!あの独特な口癖がエリートの証なんだって〜うける!',
        'エリート': 'みこちって自称エリートVTuberなんだけど、実際は愛されポンコツって感じでさ、それがまた最高なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてるの知ってる?まじ個性的!',
    }

def initialize_holomem_wiki():
    with Session() as session:
        if session.query(HolomemWiki).count() > 10: 
            update_holomem_keywords(); return
        initial_data = [
            {'member_name': 'ときのそら', 'generation': '0期生', 'description': 'ホロライブの原点であり、みんなの憧れのアイドル！歌声がまじで神がかってる！'},
            {'member_name': '宝鐘マリン', 'generation': '3期生', 'description': '自称17歳のセクシー（笑）な女海賊船長！トークも歌も面白くて、まじ天才！'},
            {'member_name': '兎田ぺこら', 'generation': '3期生', 'description': '「ぺこ」が口癖のうさ耳VTuber！いたずら好きだけど、根は優しくて面白い配信の王！'},
            {'member_name': '天音かなた', 'generation': '4期生', 'description': '天界から来た天使！パワフルな歌声と握力50kgのギャップがうける！PP天使！'},
            {'member_name': 'さくらみこ', 'generation': '0期生', 'description': '「にぇ」が口癖のエリート巫女VTuber！ポンコツかわいいところが最高なんだよね〜！'}
        ]
        for data in initial_data:
            if not session.query(HolomemWiki).filter_by(member_name=data['member_name']).first():
                session.add(HolomemWiki(**data))
        session.commit()
        update_holomem_keywords()

def update_holomem_keywords():
    global g_holomem_keywords
    with Session() as session:
        g_holomem_keywords = [row[0] for row in session.query(HolomemWiki).all()]
    logger.info(f"✅ Holomem keywords updated: {len(g_holomem_keywords)} members")

def is_holomem_name_only_request(message):
    if len(message) > 15: return None
    for name in g_holomem_keywords:
        if name in message and len(message.replace(name, "").strip()) < 5:
            return name
    return None

def get_holomem_info(session, member_name):
    return session.query(HolomemWiki).filter_by(member_name=member_name).first()

# --- コア機能 (音声, 天気, 検索) ---
def ensure_voice_directory():
    try: os.makedirs(VOICE_DIR, exist_ok=True)
    except Exception as e: logger.error(f"❌ Voice directory creation failed: {e}")
def generate_voice(text):
    if not VOICEVOX_ENABLED: return None
    try:
        voicevox_url = VOICEVOX_URL_FROM_ENV or "http://localhost:50021"
        final_text = limit_text_for_sl(text, 150)
        query_response = requests.post(f"{voicevox_url}/audio_query", params={"text": final_text, "speaker": VOICEVOX_SPEAKER_ID}, timeout=10)
        synthesis_response = requests.post(f"{voicevox_url}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=query_response.json(), timeout=30)
        synthesis_response.raise_for_status()
        filename = f"voice_{int(time.time())}_{random.randint(1000, 9999)}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(synthesis_response.content)
        return filepath
    except Exception as e:
        logger.error(f"❌ VOICEVOX generation error: {e}")
        return None
def get_weather_forecast(location):
    area_code = LOCATION_CODES.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        text = clean_text(response.json().get('text', ''))
        return f"今の{location}の天気はね、「{text}」って感じだよ！" if text else f"{location}の天気情報が見つからなかった…"
    except Exception as e:
        logger.error(f"Weather API error for {location}: {e}")
        return "うぅ、天気情報がうまく取れなかったみたい…"
def scrape_major_search_engines(query, num_results=3):
    search_configs = [
        {'name': 'Bing', 'url': f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP", 'selector': 'li.b_algo'},
        {'name': 'Yahoo', 'url': f"https://search.yahoo.co.jp/search?p={quote_plus(query)}", 'selector': 'div.Algo'},
        {'name': 'Google', 'url': f"https://www.google.com/search?q={quote_plus(query)}&hl=ja", 'selector': 'div.g'}
    ]
    for config in search_configs:
        try:
            response = requests.get(config['url'], headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            for elem in soup.select(config['selector'])[:num_results]:
                title_elem = elem.select_one('h2, h3, .LC20lb')
                snippet_elem = elem.select_one('.b_caption p, .compText, .VwiC3b')
                if title_elem and snippet_elem:
                    results.append({'title': clean_text(title_elem.get_text()), 'snippet': clean_text(snippet_elem.get_text())})
            if results: 
                logger.info(f"✅ Search successful on {config['name']}")
                return results
        except Exception as e:
            logger.warning(f"⚠️ Search failed on {config['name']}: {e}")
            continue
    return []

# --- 心理分析 ---
def analyze_user_psychology(user_uuid):
    with Session() as session:
        try:
            history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(50).all()
            if len(history) < 10 or not groq_client: return

            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            messages_text = "\n".join([h.content for h in reversed(history)])
            prompt = f"ユーザー「{user.user_name}」の会話履歴を分析し、性格を要約してJSONで返してください（例: {{\"summary\": \"明るい性格…\", \"confidence\": 80}}）: {messages_text[:2000]}"
            
            completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", response_format={"type": "json_object"})
            analysis_data = json.loads(completion.choices[0].message.content)
            
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not psych:
                psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name)
                session.add(psych)
            
            psych.analysis_summary = analysis_data.get('summary', '')
            psych.analysis_confidence = analysis_data.get('confidence', 70)
            psych.last_analyzed = datetime.utcnow()
            
            session.commit()
            logger.info(f"✅ Psychology analysis updated for {user.user_name}")
        except Exception as e:
            logger.error(f"Psychology analysis failed: {e}")

# --- AI & バックグラウンドタスク ---
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    if not groq_client: 
        return generate_fallback_response(message, reference_info)
    
    try:
        psych_prompt = ""
        try:
            with Session() as session:
                psych = session.query(UserPsychology).filter_by(user_uuid=user_data['uuid']).first()
            if psych and hasattr(psych, 'analysis_summary') and psych.analysis_summary:
                psych_prompt = f"\n# 【{user_data['name']}さんの特性】\n- {psych.analysis_summary}"
        except Exception as db_error:
            logger.warning(f"⚠️ Psychology fetch failed (continuing without): {db_error}")
        
        system_prompt = f"""あなたは「もちこ」という明るくて親しみやすいギャルAIです。{user_data['name']}さんと話しています。
# 基本的な性格:
- 一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。
- 友達のように気軽に、優しく、ノリが良い。
# 会話スタイル:
- 普段は普通の日常会話を楽しむこと（天気、食べ物、趣味、感情、世間話など）。
- 相手がホロライブの話をしていない限り、自分から話題に出さない。
- 【重要】確実な情報（参考情報やDBの情報）がない場合は、安易に断定せず「〜だと思うな」「推測だけど〜かも！」のように不確かな表現を使うか、「その情報は持ってないや、ごめんね！」と正直に答えること。
{psych_prompt}"""
        
        if is_task_report:
            system_prompt += "\n- 【今回のミッション】「おまたせ！さっきの件なんだけど…」から始めて、【参考情報】を元に自然に答える。"
        elif is_detailed: 
            system_prompt += "\n- 【専門家モード】参考情報に基づき、詳しく解説して。"
        if reference_info: 
            system_prompt += f"\n【参考情報】: {reference_info}"
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in reversed(history): 
            messages.append({"role": "assistant" if h.role == "assistant" else "user", "content": h.content})
        messages.append({"role": "user", "content": message})
        
        completion = groq_client.chat.completions.create(
            messages=messages, model="llama-3.1-8b-instant", temperature=0.8, max_tokens=400 if is_detailed else 200)
        return completion.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"❌ AI response error: {e}")
        logger.error(traceback.format_exc())
        return generate_fallback_response(message, reference_info)

def generate_fallback_response(message, reference_info=""):
    if reference_info:
        return f"調べてきたよ！\n\n{reference_info[:SL_SAFE_CHAR_LIMIT-50]}"
    greetings = {
        'こんにちは': ['やっほー！', 'こんにちは〜！元気？'], 'おはよう': ['おはよ〜！今日もいい天気だね！', 'おっはよ〜！'],
        'こんばんは': ['こんばんは！今日どうだった？', 'ばんは〜！'], 'ありがとう': ['どういたしまして！', 'いえいえ〜！'],
    }
    for keyword, responses in greetings.items():
        if keyword in message: return random.choice(responses)
    if '?' in message or '？' in message:
        return random.choice(["それ、気になるね！", "うーん、なんて言おうかな！", "まじ？どういうこと？"])
    return random.choice(["うんうん！", "なるほどね！", "そうなんだ！", "まじで？"])

def background_task_runner(task_id, query, task_type, user_uuid):
    result_data, result_status = None, 'failed'
    try:
        if task_type == 'search':
            search_query = query
            if (topic := extract_recommendation_topic(query)): search_query = f"おすすめ {topic} ランキング"
            elif (topic := detect_specialized_topic(query)): search_query = f"site:{SPECIALIZED_SITES[topic]['base_url']} {query}"
            raw_results = scrape_major_search_engines(search_query, 5)
            result_data = json.dumps(format_search_results_as_list(raw_results), ensure_ascii=False) if raw_results else None
        elif task_type == 'psych_analysis':
            analyze_user_psychology(user_uuid)
            result_data = "Analysis Complete"
        result_status = 'completed'
    except Exception as e:
        logger.error(f"❌ Background task '{task_type}' failed: {e}")
        logger.error(traceback.format_exc())

    with Session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result_data
            task.status = result_status
            task.completed_at = datetime.utcnow()
            session.commit()

def start_background_task(user_uuid, query, task_type):
    task_id = hashlib.md5(f"{user_uuid}{str(query)}{time.time()}{task_type}".encode()).hexdigest()[:10]
    with Session() as session:
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type=task_type, query=query)
        session.add(task)
        session.commit()
    background_executor.submit(background_task_runner, task_id, query, task_type, user_uuid)
    return True

# --- Flask エンドポイント ---
@app.route('/health')
def health_check():
    db_ok = 'error'
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1")); db_ok = 'ok'
    except: pass
    return jsonify({'status': 'ok', 'db': db_ok, 'ai': 'ok' if groq_client else 'disabled'})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        if not all(k in data for k in ['user_uuid', 'user_name', 'message']):
            return Response("Error: Missing required fields|", status=400, mimetype='text/plain; charset=utf-8')

        user_uuid, user_name, message = data['user_uuid'], data['user_name'], data['message'].strip()
        
        with Session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid, limit=4)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.commit()

            response_text = ""
            
            if 'ホロライブ' in message and any(kw in message for kw in ['ニュース', '最新', '情報']):
                news_items = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(5).all()
                if news_items:
                    save_news_cache(session, user_uuid, news_items)
                    save_user_context(session, user_uuid, 'hololive_news', message)
                    news_titles = [f"【{i+1}】{item.title}" for i, item in enumerate(news_items)]
                    response_text = "ホロライブの最新ニュース、こんな感じだよ！\n" + "\n".join(news_titles) + "\n\n気になる番号を教えてくれたら詳しく話すよ！"
                else:
                    start_background_task(user_uuid, "ホロライブ 最新ニュース", 'search')
                    response_text = "ごめん、今DBにニュースがないや！Webで調べてみるからちょっと待ってて！"
            elif '性格分析' in message:
                start_background_task(user_uuid, message, 'psych_analysis')
                response_text = "おっけー！あなたのこと、分析しちゃうね！ちょっと時間かかるかも！"
            elif ('さくらみこ' in message or 'みこち' in message):
                for keyword, resp in get_sakuramiko_special_responses().items():
                    if keyword in message:
                        response_text = resp; break
                if not response_text:
                    response_text = generate_ai_response(user_data, message, history, "さくらみこはホロライブ所属の人気VTuber。独特な口癖やゲーム実況が人気。")
            elif (member_name := is_holomem_name_only_request(message)):
                member_info = get_holomem_info(session, member_name)
                if member_info:
                    response_text = generate_ai_response(user_data, f"{member_name}について教えて", history, member_info.description)
                else:
                    start_background_task(user_uuid, message, 'search')
                    response_text = f"ごめん、「{message}」ちゃんの詳しい情報は持ってないや…。Webで調べてみるね！"
            elif (selected_number := is_number_selection(message)):
                user_context = get_user_context(session, user_uuid)
                
                if user_context and user_context['type'] == 'hololive_news':
                    news_detail = get_cached_news_detail(session, user_uuid, selected_number)
                    if news_detail:
                        response_text = generate_ai_response(user_data, f"{news_detail.title}について教えて", history, news_detail.content, is_detailed=True)
                    else:
                        response_text = "あれ、その番号のニュースが見つからないや…"
                else: # デフォルトをWeb検索にする
                    saved_result = get_saved_search_result(user_uuid, selected_number)
                    if saved_result:
                        prompt = f"「{saved_result['title']}」について詳しく教えて！"
                        response_text = generate_ai_response(user_data, prompt, history, saved_result['full_content'], is_detailed=True)
                    else:
                        response_text = "あれ、何の番号だっけ？もう一回検索してみて！"
            elif is_follow_up_question(message, history):
                last_assistant_msg = next((h.content for h in history if h.role == 'assistant'), "")
                response_text = generate_ai_response(user_data, message, history, f"直前の回答: {last_assistant_msg}", is_detailed=True)
            elif is_time_request(message):
                response_text = get_japan_time()
            elif (location := is_weather_request(message)):
                response_text = get_weather_forecast(location)
            elif should_search(message):
                start_background_task(user_uuid, message, 'search')
                response_text = "おっけー、調べてみるね！終わったら教える！"
            
            if not response_text:
                response_text = generate_ai_response(user_data, message, history)
            
            response_text = limit_text_for_sl(response_text)
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
            session.commit()
            
            return Response(f"{response_text}|", mimetype='text/plain; charset=utf-8')

    except Exception as e:
        logger.error(f"Chat error: {e}")
        logger.error(traceback.format_exc())
        return Response("ごめん、システムエラーが起きちゃった…|", status=500, mimetype='text/plain; charset=utf-8')

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    try:
        user_uuid = request.json['user_uuid']
        with Session() as session:
            task = session.query(BackgroundTask).filter_by(user_uuid=user_uuid, status='completed').order_by(BackgroundTask.completed_at.desc()).first()
            if not task: return jsonify({'status': 'no_tasks'})
            
            response_text = ""
            if task.task_type == 'search':
                results = json.loads(task.result) if task.result else None
                if not results:
                    response_text = f"「{task.query}」を調べたけど情報が見つからなかった…"
                else:
                    save_search_context(user_uuid, results, task.query)
                    save_user_context(session, user_uuid, 'web_search', task.query)
                    list_items = [f"【{r['number']}】{r['title']}" for r in results]
                    response_text = f"おまたせ！「{task.query}」について調べてきたよ！\n" + "\n".join(list_items) + "\n\n気になる番号教えて！"
            elif task.task_type == 'psych_analysis':
                psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
                if psych and hasattr(psych, 'analysis_summary') and psych.analysis_summary:
                    response_text = f"分析終わったよ！あてぃしが見たあなたは…「{psych.analysis_summary}」って感じ！(信頼度: {psych.analysis_confidence}%)"
                else:
                    response_text = "分析終わったけど、まだうまくまとめられないや…"
            
            response_text = limit_text_for_sl(response_text)
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
            session.delete(task)
            session.commit()
            
            return Response(json.dumps({'status': 'completed', 'response': response_text}, ensure_ascii=False), mimetype='application/json; charset=utf-8')
            
    except Exception as e:
        logger.error(f"Check task error: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

# --- アプリケーション起動 ---
def initialize_app():
    global engine, Session, groq_client
    logger.info("="*50)
    logger.info("🔧 Mochiko AI (Final Ver.) Starting Up...")
    logger.info("="*50)
    
    if GROQ_API_KEY and len(GROQ_API_KEY) > 20:
        try:
            groq_client = Groq(api_key=GROQ_API_KEY)
            logger.info("🔍 Verifying Groq API key...")
            groq_client.chat.completions.create(messages=[{"role": "user", "content": "test"}], model="llama-3.1-8b-instant", max_tokens=2)
            logger.info("✅ Groq API key is valid and working.")
        except Exception as e:
            logger.critical("🔥🔥🔥 FATAL: Groq API key verification failed! 🔥🔥🔥")
            groq_client = None
    else:
        logger.warning("⚠️ GROQ_API_KEY is not set or too short. AI features will be disabled.")
        groq_client = None

    is_sqlite = 'sqlite' in DATABASE_URL
    connect_args = {'check_same_thread': False} if is_sqlite else {}
    engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    
    initialize_holomem_wiki()
    
    def run_scheduler():
        while True: 
            schedule.run_pending()
            time.sleep(60)

    threading.Thread(target=run_scheduler, daemon=True).start()
    
    logger.info("✅ Initialization Complete!")

application = None
try:
    initialize_app()
    application = app
except Exception as e:
    logger.critical(f"Fatal init error: {e}", exc_info=True)
    application = Flask(__name__)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    application.run(host='0.0.0.0', port=port, debug=False)
