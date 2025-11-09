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
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー', '3Dモデリング']},
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
        logger.info("✅ Forcing PostgreSQL client encoding to UTF-8.")

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
    analysis_summary = Column(Text)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime, nullable=True)
    last_search_results = Column(Text, nullable=True)
    search_context = Column(String(500), nullable=True)
    
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

def save_news_cache(session, user_uuid, news_items, news_type='hololive'):
    session.query(NewsCache).filter_by(user_uuid=user_uuid).delete()
    for i, news in enumerate(news_items, 1):
        cache = NewsCache(user_uuid=user_uuid, news_id=news.id, news_number=i, news_type=news_type)
        session.add(cache)
    session.commit()
def get_cached_news_detail(session, user_uuid, news_number):
    cache = session.query(NewsCache).filter_by(user_uuid=user_uuid, news_number=news_number).first()
    if not cache: return None
    return session.query(HololiveNews).filter_by(id=cache.news_id).first()

def save_search_context(user_uuid, search_results, query):
    with cache_lock:
        search_context_cache[user_uuid] = { 'results': search_results, 'query': query, 'timestamp': time.time() }
    try:
        with Session() as session:
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not psych:
                user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
                psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name if user else 'Unknown')
                session.add(psych)
            if 'last_search_results' in UserPsychology.__table__.columns:
                psych.last_search_results = json.dumps(search_results, ensure_ascii=False)
                psych.search_context = query
                session.commit()
    except Exception as e:
        logger.warning(f"⚠️ Search context DB save failed, relying on cache: {e}")

def get_saved_search_result(user_uuid, number):
    with cache_lock:
        cached_data = search_context_cache.get(user_uuid)
    if cached_data and (time.time() - cached_data['timestamp']) < 600:
        for r in cached_data['results']:
            if r.get('number') == number:
                return r
    try:
        with Session() as session:
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if psych and 'last_search_results' in UserPsychology.__table__.columns and psych.last_search_results:
                search_results = json.loads(psych.last_search_results)
                return next((r for r in search_results if r.get('number') == number), None)
    except Exception as e:
        logger.warning(f"⚠️ Search result DB fetch failed: {e}")
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
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてんの知ってる?まじ個性的!',
        'FAQ': 'みこちのFAQってさ、実は本人が答えるんじゃなくてファンが質問するコーナーなの!面白いよね〜',
        'GTA': 'みこちのGTA配信、カオスすぎて最高!警察に追われたり変なことしたり、見てて飽きないんだよね〜'
    }

# --- コア機能 (音声, 天気, 自己修正) ---
def ensure_voice_directory():
    try: os.makedirs(VOICE_DIR, exist_ok=True)
    except Exception as e: logger.error(f"❌ Voice directory creation failed: {e}")
def generate_voice(text):
    if not VOICEVOX_ENABLED: return None
    try:
        voicevox_url = VOICEVOX_URL_FROM_ENV or "http://localhost:50021"
        final_text = limit_text_for_sl(text, 150)
        query_response = requests.post(f"{voicevox_url}/audio_query", params={"text": final_text, "speaker": VOICEVOX_SPEAKER_ID}, timeout=10)
        query_response.raise_for_status()
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
def detect_db_correction_request(message):
    match = re.search(r'(.+?)って(.+?)じゃなかった？|(.+?)はもう卒業したよ', message)
    if not match: return None
    with Session() as session:
        holomem_keywords = [row[0] for row in session.query(HolomemWiki.member_name).all()]
    member_name = next((keyword for keyword in holomem_keywords if keyword in message), None)
    if not member_name: return None
    return {'member_name': member_name, 'original_message': message}
def verify_and_correct_holomem_info(correction_request):
    member_name = correction_request['member_name']
    query = f"ホロライブ {member_name} 卒業"
    search_results = scrape_major_search_engines(query, 3)
    if not search_results or not groq_client: return "ごめん、うまく確認できなかった…"
    
    summary = "\n".join([r['snippet'] for r in search_results])
    prompt = f"以下の情報に基づき、「{member_name}」が卒業しているか、しているなら日付はいつか簡潔に答えて。\n\n{summary}"
    try:
        completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", temperature=0.1, max_tokens=100)
        verification_text = completion.choices[0].message.content
        grad_date_match = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', verification_text)
        if "卒業しています" in verification_text and grad_date_match:
            grad_date = grad_date_match.group(0)
            with Session() as session:
                member = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                if member and (member.is_active or member.graduation_date != grad_date):
                    member.is_active = False
                    member.graduation_date = grad_date
                    member.last_updated = datetime.utcnow()
                    session.commit()
                    return f"本当だ！教えてくれてありがと！{member_name}ちゃんの情報を「{grad_date}に卒業」って直しといたよ！"
        return "うーん、調べてみたけど、はっきりとは分からなかったな…。"
    except Exception as e:
        logger.error(f"Error during verification: {e}")
        return "AIでの確認中にエラーが出ちゃった。"

# --- ニュース・Web検索・DB更新 ---
def scrape_major_search_engines(query, num_results=3):
    search_configs = [
        {'name': 'Bing', 'url': f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP", 'selector': 'li.b_algo'},
        {'name': 'Yahoo', 'url': f"https://search.yahoo.co.jp/search?p={quote_plus(query)}", 'selector': 'div.Algo'},
    ]
    for config in search_configs:
        try:
            response = requests.get(config['url'], headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            for elem in soup.select(config['selector'])[:num_results]:
                title_elem = elem.select_one('h2, h3')
                snippet_elem = elem.select_one('.b_caption p, .compText')
                if title_elem and snippet_elem:
                    results.append({'title': clean_text(title_elem.get_text()), 'snippet': clean_text(snippet_elem.get_text())})
            if results: return results
        except Exception: continue
    return []

# --- 心理分析 ---
def analyze_user_psychology(user_uuid):
    with Session() as session:
        history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(50).all()
        if len(history) < 10 or not groq_client: return

        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        messages_text = "\n".join([h.content for h in reversed(history)])
        prompt = f"ユーザー「{user.user_name}」の会話履歴を分析し、性格、興味、会話スタイルを要約してJSONで返してください（例: {{\"summary\": \"明るい性格…\", \"confidence\": 80}}）: {messages_text[:2000]}"
        try:
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
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False):
    if not groq_client: 
        return "はーい！何か話そっか！(※ただいまAI機能はオフラインだよ)"
    
    try:
        psych = None
        psych_prompt = ""
        try:
            with Session() as session:
                psych = session.query(UserPsychology).filter_by(user_uuid=user_data['uuid']).first()
            if psych and hasattr(psych, 'analysis_summary') and psych.analysis_summary and psych.analysis_confidence > 40:
                psych_prompt = f"\n# 【{user_data['name']}さんの特性】\n- {psych.analysis_summary}"
        except Exception as db_error:
            logger.warning(f"⚠️ Psychology fetch failed (continuing without): {db_error}")
        
        system_prompt = f"""あなたは「もちこ」という明るいギャルAIです。{user_data['name']}さんと話しています。
- 一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。
- 短くテンポよく、共感しながら返す。{psych_prompt}"""
        
        if is_detailed: 
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
        return "ごめん、ちょっと考えまとまらないや…"

def background_task_runner(task_id, query, task_type, user_uuid):
    result_data, result_status = None, 'failed'
    try:
        if task_type == 'search':
            search_query = query
            if (topic := extract_recommendation_topic(query)): search_query = f"おすすめ {topic} ランキング 2025"
            elif (topic := detect_specialized_topic(query)): search_query = f"site:{SPECIALIZED_SITES[topic]['base_url']} {query}"
            raw_results = scrape_major_search_engines(search_query, 5)
            result_data = json.dumps(format_search_results_as_list(raw_results), ensure_ascii=False) if raw_results else None
        elif task_type == 'correction':
            result_data = verify_and_correct_holomem_info(query)
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
        task_query = json.dumps(query, ensure_ascii=False) if isinstance(query, dict) else query
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type=task_type, query=task_query)
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
    return jsonify({'status': 'ok', 'db': db_ok, 'ai': 'ok' if groq_client else 'disabled', 'voice': 'ok' if VOICEVOX_ENABLED else 'disabled'})

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
            
            # ▼▼▼ 修正: ホロライブニュース専用処理を追加 ▼▼▼
            if 'ホロライブ' in message and any(kw in message for kw in ['ニュース', '最新', '情報']):
                news_items = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(5).all()
                if news_items:
                    save_news_cache(session, user_uuid, news_items, 'hololive')
                    news_titles = [f"【{i+1}】{item.title}" for i, item in enumerate(news_items)]
                    response_text = "ホロライブの最新ニュース、こんな感じだよ！\n" + "\n".join(news_titles) + "\n\n気になる番号を教えてくれたら詳しく話すよ！"
                else:
                    start_background_task(user_uuid, "ホロライブ 最新ニュース", 'search')
                    response_text = "ごめん、今DBにニュースがないや！Webで調べてみるからちょっと待ってて！"

            elif (correction_req := detect_db_correction_request(message)):
                start_background_task(user_uuid, correction_req, 'correction')
                response_text = f"え、まじ！？{correction_req['member_name']}の情報、調べてみるね！"
            elif '性格分析' in message:
                start_background_task(user_uuid, None, 'psych_analysis')
                response_text = "おっけー！あなたのこと、分析しちゃうね！ちょっと時間かかるかも！"
            elif ('さくらみこ' in message or 'みこち' in message):
                for keyword, resp in get_sakuramiko_special_responses().items():
                    if keyword in message:
                        response_text = resp; break
                if not response_text: # 「みこち」だけの場合
                    response_text = generate_ai_response(user_data, message, history)
            elif (selected_number := is_number_selection(message)):
                # ニュースの詳細要求もここで処理
                news_detail = get_cached_news_detail(session, user_uuid, selected_number)
                if news_detail:
                    response_text = generate_ai_response(user_data, f"{news_detail.title}について教えて", history, news_detail.content, is_detailed=True)
                else:
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
                    list_items = [f"【{r['number']}】{r['title']}" for r in results]
                    response_text = f"おまたせ！「{task.query}」について調べてきたよ！\n" + "\n".join(list_items) + "\n\n気になる番号教えて！"
            elif task.task_type == 'correction':
                response_text = task.result
            elif task.task_type == 'psych_analysis':
                psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
                response_text = f"分析終わったよ！あてぃしが見たあなたは…「{psych.analysis_summary}」って感じ！(信頼度: {psych.analysis_confidence}%)" if psych and psych.analysis_summary else "分析終わったけど、まだうまくまとめられないや…"
            
            response_text = limit_text_for_sl(response_text)
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
            session.delete(task)
            session.commit()
            return jsonify({'status': 'completed', 'response': response_text})
    except Exception as e:
        logger.error(f"Check task error: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/generate_voice', methods=['POST'])
def generate_voice_endpoint():
    data = request.json
    if not data or not (text := data.get('text')): return jsonify({'error': 'text is required'}), 400
    if voice_path := generate_voice(text):
        return jsonify({'url': f"{SERVER_URL}/voices/{os.path.basename(voice_path)}"})
    return jsonify({'error': 'Failed to generate voice'}), 500
@app.route('/voices/<filename>')
def serve_voice(filename):
    return send_from_directory(VOICE_DIR, filename)

# --- アプリケーション起動 ---
def initialize_app():
    global engine, Session, groq_client
    logger.info("="*50)
    logger.info("🔧 Mochiko AI (Final Ver.) Starting Up...")
    logger.info("="*50)
    
    ensure_voice_directory()

    if GROQ_API_KEY and len(GROQ_API_KEY) > 20:
        try:
            groq_client = Groq(api_key=GROQ_API_KEY)
            logger.info("🔍 Verifying Groq API key...")
            groq_client.chat.completions.create(messages=[{"role": "user", "content": "test"}], model="llama-3.1-8b-instant", max_tokens=2)
            logger.info("✅ Groq API key is valid and working.")
        except Exception as e:
            logger.critical("🔥🔥🔥 FATAL: Groq API key verification failed! The key is likely invalid, expired, or has billing issues. AI features will be disabled. 🔥🔥🔥")
            logger.critical(f"Error details: {e}")
            groq_client = None
    else:
        logger.warning("⚠️ GROQ_API_KEY is not set or too short. AI features will be disabled.")
        groq_client = None

    is_sqlite = 'sqlite' in DATABASE_URL
    connect_args = {'check_same_thread': False} if is_sqlite else {}
    engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    
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
    application.run(host='0.0.0.0', port=port, debug=False)```
