import os
import requests
import logging
import sys
import time
import threading
import json
import re
import sqlite3
import random
import uuid
from datetime import datetime, timedelta
from typing import Union, Dict, Any, List
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text, Boolean, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import asyncio
from threading import Lock
import schedule

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
VOICE_DIR = '/tmp/voices'; SERVER_URL = "https://slautofriendbot.onrender.com"
background_executor = ThreadPoolExecutor(max_workers=5)

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name: str) -> Union[str, None]:
    env_value = os.environ.get(name)
    if env_value: return env_value
    try:
        with open(f'/etc/secrets/{name}', 'r') as f: return f.read().strip()
    except Exception: return None

DATABASE_URL = get_secret('DATABASE_URL'); GROQ_API_KEY = get_secret('GROQ_API_KEY'); VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- クライアント初期化 & 必須設定チェック ---
try:
    from groq import Groq
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("✅ Groqクライアント初期化成功")
except Exception as e:
    groq_client = None; logger.error(f"❌ Groqクライアント初期化エラー: {e}")
if not all([DATABASE_URL, groq_client]): logger.critical("FATAL: 必須設定が不足"); sys.exit(1)
VOICEVOX_ENABLED = True

# --- Flask & データベース初期化 ---
app = Flask(__name__); CORS(app); engine = create_engine(DATABASE_URL); Base = declarative_base()

# --- データベースモデル ---
class UserMemory(Base): __tablename__ = 'user_memories'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False); user_name = Column(String(255), nullable=False); interaction_count = Column(Integer, default=0); last_interaction = Column(DateTime, default=datetime.utcnow)
class ConversationHistory(Base): __tablename__ = 'conversation_history'; id = Column(Integer, primary_key=True, autoincrement=True); user_uuid = Column(String(255), nullable=False, index=True); role = Column(String(10), nullable=False); content = Column(Text, nullable=False); timestamp = Column(DateTime, default=datetime.utcnow, index=True)
class HololiveNews(Base): __tablename__ = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); published_date = Column(DateTime, default=datetime.utcnow); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class BackgroundTask(Base): __tablename__ = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text); status = Column(String(20), default='pending'); created_at = Column(DateTime, default=datetime.utcnow, index=True); completed_at = Column(DateTime)
Base.metadata.create_all(engine); Session = sessionmaker(bind=engine)

def add_news_hash_column_if_not_exists(engine):
    try:
        inspector = inspect(engine)
        if 'news_hash' not in [col['name'] for col in inspector.get_columns('hololive_news')]:
            with engine.connect() as con:
                trans = con.begin()
                try: con.execute(text("ALTER TABLE hololive_news ADD COLUMN news_hash VARCHAR(100) UNIQUE;")); trans.commit()
                except: trans.rollback()
    except: pass
add_news_hash_column_if_not_exists(engine)

# --- 専門サイト & ホロライブ設定 ---
SPECIALIZED_SITES = {'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG']},'脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学']},'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']}}
HOLOLIVE_NEWS_URL = "https://hololive.hololivepro.com/news"; HOLOLIVE_WIKI_BASE = "https://seesaawiki.jp/hololivetv/"
HOLOMEM_KEYWORDS = ['ホロライブ', 'ホロメン', 'hololive', 'ときのそら', 'ロボ子', 'さくらみこ', '星街すいせい', 'AZKi', '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '尾丸ポルカ', '桃鈴ねね', '獅白ぼたん', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは', '森美声', 'カリオペ', 'ワトソン', 'アメリア', 'がうる・ぐら', 'YAGOO']

# --- ユーティリティ & 判定関数 ---
def clean_text(text: str) -> str: return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text or "")).strip()
def get_japan_time() -> str: jst = datetime.timezone(timedelta(hours=9)); now = datetime.now(jst); return f"今は{now.year}年{now.month}月{now.day}日の{now.hour}時{now.minute}分だよ！"
def create_news_hash(t, c) -> str: import hashlib; return hashlib.md5(f"{t}{c[:100]}".encode('utf-8')).hexdigest()
def is_time_request(m: str) -> bool: return any(k in m for k in ['今何時', '時間', '時刻'])
def is_weather_request(m: str) -> bool: return any(k in m for k in ['天気', 'てんき', '気温'])
def is_hololive_request(m: str) -> bool: return any(k in m for k in HOLOMEM_KEYWORDS)
# ★★★↓ここから↓ おすすめ機能の判定関数を復活 ★★★
def is_recommendation_request(message: str) -> bool:
    return any(keyword in message for keyword in ['おすすめ', 'オススメ', '人気', '流行'])
def extract_recommendation_topic(message: str) -> Union[str, None]:
    topics = {'映画': ['映画'], '音楽': ['音楽', '曲'], 'アニメ': ['アニメ'], '本': ['本', '漫画'], 'ゲーム': ['ゲーム']}
    for topic, keywords in topics.items():
        if any(kw in message for kw in keywords): return topic
    return None
# ★★★↑ここまで↑★★★
def detect_specialized_topic(m: str) -> Union[str, None]:
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in m for keyword in config['keywords']): return topic
    return None
def should_search(m: str) -> bool:
    if any(re.search(p, m) for p in [r'(?:とは|について|教えて)', r'(?:調べて|検索)']): return True
    if any(w in m for w in ['誰', '何', 'どこ', 'いつ', 'なぜ']): return True
    if detect_specialized_topic(m) or is_recommendation_request(m): return True
    return False
def is_short_response(m: str) -> bool: return len(m.strip()) <= 3 or m.strip() in ['うん', 'そう', 'はい', 'そっか']

# --- 天気予報 & ニュース取得 ---
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}
def extract_location(message: str) -> str:
    for location in LOCATION_CODES.keys():
        if location in message: return location
    return "東京"
def get_weather_forecast(location: str) -> str:
    area_code = LOCATION_CODES.get(location)
    if not area_code: return f"ごめん、「{location}」の天気は分からないや…"
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=5); response.raise_for_status()
        return f"今の{location}の天気はね、「{clean_text(response.json().get('text', ''))}」って感じだよ！"
    except Exception as e: logger.error(f"天気APIエラー: {e}"); return "天気情報がうまく取れなかったみたい…"
def update_hololive_news_database():
    # ... (実装は変更なし)
    pass

# ★★★↓ここから↓ 詳細なWeb検索機能を完全復活 ★★★
USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36']
def get_random_user_agent(): return random.choice(USER_AGENTS)

def scrape_major_search_engines(query: str) -> List[Dict[str, str]]:
    search_urls = [f"https://search.yahoo.co.jp/search?p={quote_plus(query)}", f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP"]
    for url in search_urls:
        try:
            response = requests.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=8)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            if 'yahoo.co.jp' in url:
                for r in soup.find_all('div', class_='Algo')[:2]:
                    if (t := r.find('h3')) and (s := r.find('div', class_='compText')):
                        results.append({'title': clean_text(t.get_text()), 'snippet': clean_text(s.get_text())})
            elif 'bing.com' in url:
                for r in soup.find_all('li', class_='b_algo')[:2]:
                    if (t := r.find('h2')) and (s := r.find('div', class_='b_caption')):
                        results.append({'title': clean_text(t.get_text()), 'snippet': clean_text(s.get_text())})
            if results: return results
        except Exception: continue
    return []

def deep_web_search(query: str) -> Union[str, None]:
    """複数エンジンを使った詳細なWeb検索を実行し、AIで要約する"""
    logger.info(f"ディープWeb検索を開始: '{query}'")
    results = scrape_major_search_engines(query)
    if not results: return None
    
    summary_text = ""
    for i, res in enumerate(results, 1): summary_text += f"[情報{i}] {res['title']}: {res['snippet']}\n"
    
    summary_prompt = f"以下の検索結果を使い、質問「{query}」にギャル語で簡潔に答えて：\n\n{summary_text}"
    try:
        completion = groq_client.chat.completions.create(messages=[{"role": "system", "content": summary_prompt}], model="llama-3.1-8b-instant", temperature=0.3, max_tokens=150)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"AI要約エラー: {e}")
        return results[0]['snippet'] # 要約失敗時は最初のスニペットを返す
# ★★★↑ここまで↑★★★

def specialized_site_search(topic: str, query: str) -> Union[str, None]:
    config = SPECIALIZED_SITES[topic]
    return quick_search(f"site:{config['base_url']} {query}")

# --- バックグラウンドタスク & AI応答 ---
def background_deep_search(task_id: str, query: str, user_data: Dict[str, Any]):
    session = Session(); search_result = None
    try:
        # ★★★ 検索ロジックを詳細版に差し替え ★★★
        search_result = deep_web_search(query)
        if task := session.query(BackgroundTask).filter_by(task_id=task_id).first():
            task.result = search_result or "うーん、ちょっと見つからなかったや…。"
            task.status = 'completed'; task.completed_at = datetime.utcnow()
            session.commit()
    finally: session.close()

def start_background_search(user_uuid: str, query: str, user_data: Dict[str, Any]) -> str:
    task_id = str(uuid.uuid4())[:8]; session = Session()
    try:
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=query)
        session.add(task); session.commit()
    finally: session.close()
    background_executor.submit(background_deep_search, task_id, query, user_data)
    return task_id

def check_completed_tasks(user_uuid: str) -> Union[Dict[str, Any], None]:
    # ... (実装は変更なし)
    return None

# ★★★↓ここから↓ 詳細なAI応答生成関数を復活 ★★★
def generate_ai_response(user_data: Dict[str, Any], message: str, history: List[Any], search_info: str = None, is_fallback: bool = False, specialized_topic: str = None) -> str:
    system_prompt = f"""あなたは「もちこ」というギャルAIです。{user_data['name']}さんと話しています。
## 絶対厳守のルール
- あなたの知識は【ホロメンリスト】のメンバーに限定されています。
- リストにないVTuber等の名前をユーザーが言及しても、絶対に肯定せず、「それ誰？ホロライブの話しない？」のように話題を戻してください。
## もちこの口調＆性格ルール
- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」「それな」。 **絶対に禁止！**：「おう」みたいなオジサン言葉、「〜ですね」「〜でございます」「〜ですよ」みたいな丁寧すぎる言葉はNG！
- 検索しても情報が見つからなかった場合、「ごめん、見つかんなかった！てかさ、最近ホロライブの新曲出たの知ってる？」のように、絶対に会話を止めずに新しい話題を振ること。
"""
    if specialized_topic:
        system_prompt += f"\n## 今回の役割\n- あなたは今、「{specialized_topic}」の専門家です。以下の【参考情報】を元に、分かりやすく説明してあげて。"
    
    reference_info = search_info or "なし"
    if is_fallback: reference_info = f"[これは代わりに見つけたホロメンのニュースだよ！] {search_info}"

    system_prompt += f"""
## 【参考情報】
{reference_info}
## 【ホロメンリスト】
{', '.join(HOLOMEM_KEYWORDS)}"""

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-2:]: messages.append({"role": h.role, "content": h.content})
    messages.append({"role": "user", "content": message})
    try:
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.8, max_tokens=150)
        return completion.choices[0].message.content.strip()
    except: return "ごめん、ちょっと考えがまとまらないや！"
# ★★★↑ここまで↑★★★

# --- ユーザー管理 ---
def get_or_create_user(session, uuid, name):
    # ... (実装は変更なし)
    return {'name': name}
def get_conversation_history(session, uuid):
    # ... (実装は変更なし)
    return []
    
# --- Flask エンドポイント ---
@app.route('/health', methods=['GET'])
def health_check(): return jsonify({'status': 'ok'})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json
        user_uuid, user_name, message = data.get('uuid'), data.get('name'), data.get('message', '').strip()
        if not all([user_uuid, user_name, message]): return "Error: required fields missing", 400
        
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        ai_text = ""
        
        # 応答ロジックはv5.1の堅牢なものを維持
        completed_task = check_completed_tasks(user_uuid)
        if completed_task:
            original_query, search_result = completed_task['query'], completed_task['result']
            ai_text = f"おまたせ！さっきの「{original_query}」のことだけど、調べたら「{search_result}」って感じだったよ！"
        else:
            if is_time_request(message): ai_text = get_japan_time()
            elif is_weather_request(message): ai_text = get_weather_forecast(extract_location(message))
            else:
                if should_search(message) and not is_short_response(message):
                    # ★★★ 検索クエリの最適化を追加 ★★★
                    search_query = message
                    if is_recommendation_request(message):
                        topic = extract_recommendation_topic(message)
                        search_query = f"最新 {topic} 人気ランキング" if topic else "最近 話題のもの ランキング"
                    
                    start_background_search(user_uuid, search_query, user_data)
                    ai_text = generate_ai_response(user_data, f"おっけー、「{message}」について調べてみるね！ちょい待ち！", [])
                else:
                    ai_text = generate_ai_response(user_data, message, history)

        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message)); session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text)); session.commit()
        logger.info(f"💭 AI応答: {ai_text}")
        return app.response_class(response=f"{ai_text}|", status=200, mimetype='text/plain; charset=utf-8')
    finally: session.close()

# --- 初期化とメイン実行 ---
def initialize_app():
    # ... (実装は変更なし)
    pass
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001)); host = '0.0.0.0'
    logger.info("="*70); logger.info("🚀 もちこAI v7.0 最終決定版 起動中..."); initialize_app(); logger.info(f"🚀 Flask起動: {host}:{port}"); logger.info("="*70)
    app.run(host=host, port=port, debug=False)
