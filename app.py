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
from datetime import datetime, timedelta, timezone
from groq import Groq
from flask import Response, send_from_directory
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import schedule
import signal

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
    # ▼▼▼ 修正箇所 ▼▼▼
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

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name):
    path = f"/etc/secrets/{name}"
    if os.path.exists(path):
        try:
            with open(path, 'r') as f: return f.read().strip()
        except IOError: return None
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- データベースモデル (全機能統合) ---
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
    openness = Column(Integer, default=50)
    conscientiousness = Column(Integer, default=50)
    extraversion = Column(Integer, default=50)
    agreeableness = Column(Integer, default=50)
    neuroticism = Column(Integer, default=50)
    analysis_summary = Column(Text)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime)
    last_search_results = Column(Text)
    search_context = Column(String(500))
    
class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False, index=True)
    task_type = Column(String(50), nullable=False)
    query = Column(Text, nullable=False)
    result = Column(Text)
    status = Column(String(20), default='pending', index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

class HolomemWiki(Base):
    __tablename__ = 'holomem_wiki'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text)
    debut_date = Column(String(100))
    generation = Column(String(100))
    tags = Column(Text)
    is_active = Column(Boolean, default=True, index=True)
    profile_url = Column(String(500))
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

class SpecializedNews(Base):
    __tablename__ = 'specialized_news'
    id = Column(Integer, primary_key=True)
    site_name = Column(String(100), nullable=False, index=True)
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
def create_news_hash(title, content): return hashlib.md5(f"{title}{content[:100]}".encode('utf-8')).hexdigest()

def is_detailed_request(message): return any(kw in message for kw in ['詳しく', '詳細', 'くわしく', '教えて', '説明して', '解説して', 'どういう', 'なぜ', 'どうして'])
def is_number_selection(message):
    match = re.match(r'^\s*([1-9])', message.strip())
    return int(match.group(1)) if match else None
def format_search_results_as_list(results):
    if not results: return None
    return [{'number': i, 'title': r.get('title', ''), 'snippet': r.get('snippet', ''), 'full_content': r.get('snippet', '')} for i, r in enumerate(results[:5], 1)]
def save_search_context(user_uuid, search_results, query):
    with Session() as session:
        psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        if not psych:
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name if user else 'Unknown')
            session.add(psych)
        psych.last_search_results = json.dumps(search_results, ensure_ascii=False)
        psych.search_context = query
        session.commit()
def get_saved_search_result(user_uuid, number):
    with Session() as session:
        psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        if not psych or not psych.last_search_results: return None
        search_results = json.loads(psych.last_search_results)
        return next((r for r in search_results if r.get('number') == number), None)
def is_recommendation_request(message): return any(kw in message for kw in ['おすすめ', 'オススメ', '人気'])
def extract_recommendation_topic(message):
    topics = {'映画': ['映画'], '音楽': ['音楽', '曲'], 'アニメ': ['アニメ'], 'ゲーム': ['ゲーム']}
    return next((topic for topic, keywords in topics.items() if any(kw in message for kw in keywords)), None)
def detect_specialized_topic(message):
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']): return topic
    return None

def is_time_request(message): return any(kw in message for kw in ['今何時', '時間', '時刻'])
def is_weather_request(message):
    if any(t in message for t in ['天気', '気温']): return next((loc for loc in LOCATION_CODES if loc in message), "東京")
    return None
def is_news_detail_request(message):
    match = re.search(r'([1-9]|[１-９])番', message)
    if match and any(keyword in message for keyword in ['詳しく', '詳細']): return int(unicodedata.normalize('NFKC', match.group(1)))
    return None
def is_follow_up_question(message, history):
    if not history: return False
    return any(re.search(p, message) for p in [r'もっと詳しく', r'それについて詳しく', r'なんで？', r'どういうこと'])
def should_search(message):
    if len(message) < 5 or is_number_selection(message): return False
    if detect_specialized_topic(message) or is_recommendation_request(message): return True
    if any(re.search(p, message) for p in [r'とは', r'について', r'教えて', r'最新', r'調べて', r'検索']): return True
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
    return session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()[::-1]

# --- 音声合成機能 ---
def ensure_voice_directory():
    try:
        os.makedirs(VOICE_DIR, exist_ok=True)
    except Exception as e:
        logger.error(f"❌ Voice directory creation failed: {e}")
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

# --- 自己修正機能 ---
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
    except Exception:
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
def fetch_article_content(url):
    try:
        response = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = soup.select('article p, .entry-content p, .post-content p')
        return ' '.join([clean_text(p.get_text()) for p in paragraphs])[:2000]
    except Exception: return ""
def _update_news_database(session, Model, site_name, base_url, selectors):
    try:
        response = requests.get(base_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        added_count = 0
        for article in soup.select(selectors)[:5]:
            title_elem = article.select_one('h2, h3, a')
            link_elem = article.find('a', href=True)
            if not title_elem or not link_elem: continue
            title, url = clean_text(title_elem.get_text()), urljoin(base_url, link_elem['href'])
            if not title or session.query(Model).filter_by(url=url).first(): continue
            content = fetch_article_content(url)
            summary = content[:200]
            news_item_data = {'title': title, 'content': summary, 'url': url, 'news_hash': create_news_hash(title, summary)}
            if site_name: news_item_data['site_name'] = site_name
            session.add(Model(**news_item_data))
            added_count += 1
        if added_count > 0:
            session.commit()
            logger.info(f"✅ Added {added_count} news for {site_name or 'Hololive'}")
    except Exception as e:
        logger.error(f"Error updating news for {site_name or 'Hololive'}: {e}")
        session.rollback()
def update_hololive_news():
    with Session() as session: _update_news_database(session, HololiveNews, None, "https://hololive-tsuushin.com/category/holonews/", "article.post-list")
def update_specialized_news():
    for site, config in SPECIALIZED_SITES.items():
        with Session() as session: _update_news_database(session, SpecializedNews, site, config['base_url'], "article, .post, .entry")
def scrape_hololive_members():
    # 実際にはここに mochiko_fixed11.2.TXT のスクレイピングロジックを実装します
    logger.info("Skipping member scrape in this example.")
    pass
def update_all_hololive_data():
    logger.info("🔄 Starting Holo-data sync...")
    scrape_hololive_members()
    update_hololive_news()
    logger.info("✅ Holo-data sync finished.")

# --- AI & バックグラウンドタスク ---
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False):
    if not groq_client: return "ちょっと考え中..."
    try:
        with Session() as session:
            psych = session.query(UserPsychology).filter_by(user_uuid=user_data['uuid']).first()
        psych_prompt = f"\n# 【{user_data['name']}さんの特性】\n- {psych.analysis_summary}" if psych and psych.analysis_confidence > 40 else ""
        system_prompt = f"""あなたは「もちこ」という明るいギャルAIです。{user_data['name']}さんと話しています。
- 一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。
- 短くテンポよく、共感しながら返す。{psych_prompt}"""
        if is_detailed: system_prompt += "\n- 【専門家モード】参考情報に基づき、詳しく解説して。"
        if reference_info: system_prompt += f"\n【参考情報】: {reference_info}"
        messages = [{"role": "system", "content": system_prompt}] + [{"role": "assistant" if h.role == "assistant" else "user", "content": h.content} for h in history] + [{"role": "user", "content": message}]
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.8, max_tokens=400 if is_detailed else 200)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI response error: {e}")
        return "ごめん、ちょっと考えまとまらないや…"

def background_task_runner(task_id, query, task_type, user_uuid):
    result_data, result_status = None, 'failed'
    if task_type == 'search':
        search_query = query
        if (topic := extract_recommendation_topic(query)): search_query = f"おすすめ {topic} ランキング 2025"
        elif (topic := detect_specialized_topic(query)): search_query = f"site:{SPECIALIZED_SITES[topic]['base_url']} {query}"
        raw_results = scrape_major_search_engines(search_query, 5)
        result_data = json.dumps(format_search_results_as_list(raw_results), ensure_ascii=False) if raw_results else None
        result_status = 'completed'
    elif task_type == 'correction':
        result_data = verify_and_correct_holomem_info(query)
        result_status = 'completed'
    elif task_type == 'psych_analysis':
        analyze_user_psychology(user_uuid)
        return
    
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
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type=task_type, query=json.dumps(query, ensure_ascii=False) if isinstance(query, dict) else query)
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
        user_uuid, user_name, message = data['user_uuid'], data['user_name'], data['message'].strip()
        with Session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid, limit=4)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.commit()

            response_text = ""
            is_detailed = is_detailed_request(message)

            if correction_req := detect_db_correction_request(message):
                start_background_task(user_uuid, correction_req, 'correction')
                response_text = f"え、まじ！？{correction_req['member_name']}の情報、調べてみるね！"
            elif '性格分析' in message:
                start_background_task(user_uuid, None, 'psych_analysis')
                response_text = "おっけー！あなたのこと、分析しちゃうね！ちょっと時間かかるかも！"
            elif (selected_number := is_number_selection(message)):
                saved_result = get_saved_search_result(user_uuid, selected_number)
                if saved_result:
                    prompt = f"「{saved_result['title']}」について詳しく教えて！"
                    response_text = generate_ai_response(user_data, prompt, history, saved_result['full_content'], is_detailed=True)
            elif is_follow_up_question(message, history):
                last_assistant_msg = next((h.content for h in reversed(history) if h.role == 'assistant'), "")
                response_text = generate_ai_response(user_data, message, history, f"直前の回答: {last_assistant_msg}", is_detailed=True)
            elif (location := is_weather_request(message)):
                from weather_api import get_weather_forecast # 仮
                response_text = get_weather_forecast(location)
            elif should_search(message):
                start_background_task(user_uuid, message, 'search')
                response_text = "おっけー、調べてみるね！終わったら教える！"
            else:
                response_text = generate_ai_response(user_data, message, history)
            
            response_text = limit_text_for_sl(response_text)
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
            session.commit()
            return jsonify({'response': response_text})
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

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
                response_text = f"分析終わったよ！あてぃしが見たあなたは…「{psych.analysis_summary}」って感じ！(信頼度: {psych.analysis_confidence}%)" if psych else "分析終わったけど結果が見つからないや…"
            
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
    logger.info("="*30 + "\n🔧 Mochiko AI (Ultimate Ver.) Starting Up...\n" + "="*30)
    ensure_voice_directory()
    groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
    
    is_sqlite = 'sqlite' in DATABASE_URL
    connect_args = {'check_same_thread': False} if is_sqlite else {}
    engine = create_engine(DATABASE_URL, connect_args=connect_args)

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    
    schedule.every(4).hours.do(update_all_hololive_data)
    schedule.every(6).hours.do(update_specialized_news)
    schedule.every().day.at("04:00").do(lambda: logger.info("Cleanup placeholder")) # cleanup_old_data
    
    def run_scheduler():
        while True: schedule.run_pending(); time.sleep(60)
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
