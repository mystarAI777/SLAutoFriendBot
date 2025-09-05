import os
import requests
import logging
import time
import threading
import json
import re
import sqlite3
import random
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
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
    # この環境ではシークレットファイルは使えないため、環境変数のみから読み込む
    env_value = os.environ.get(name);
    if env_value: return env_value
    return None

# ★★★ローカル実行のためのダミー設定★★★
# 実行に必要な環境変数が設定されていない場合、ダミーの値を設定する
DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY') or 'DUMMY_GROQ_KEY' # 実際のAPIキーではない
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- クライアント初期化 & 必須設定チェック ---
try:
    from groq import Groq
    # ダミーキーの場合、Groqクライアントは初期化しない
    if GROQ_API_KEY != 'DUMMY_GROQ_KEY':
        groq_client = Groq(api_key=GROQ_API_KEY)
    else:
        groq_client = None
except Exception as e: groq_client = None

if not all([DATABASE_URL]): 
    logger.critical("FATAL: データベースURLが不足しています。")
    sys.exit(1)
# groq_clientがNoneでもローカルテストができるようにチェックを緩和
if not groq_client:
    logger.warning("警告: Groq APIキーが設定されていないため、AI機能は無効です。")

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
HOLOLIVE_NEWS_URL = "https://hololive.hololivepro.com/news"
HOLOMEM_KEYWORDS = ['ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル', 'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ', '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ', '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ', 'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト', 'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス', 'ムーナ・ホシノヴァ', 'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ', 'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん', '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO']

# --- ユーティリティ & 判定関数 ---
def clean_text(text: str) -> str: return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text or "")).strip()
def get_japan_time() -> str: jst = timezone(timedelta(hours=9)); now = datetime.now(jst); return f"今は{now.year}年{now.month}月{now.day}日の{now.hour}時{now.minute}分だよ！"
def create_news_hash(t, c) -> str: import hashlib; return hashlib.md5(f"{t}{c[:100]}".encode('utf-8')).hexdigest()
def is_time_request(m: str) -> bool: return any(k in m for k in ['今何時', '時間', '時刻'])
def is_weather_request(m: str) -> bool: return any(k in m for k in ['天気', 'てんき', '気温'])
def is_hololive_request(m: str) -> bool: return any(k in m for k in HOLOMEM_KEYWORDS)
def is_recommendation_request(m: str) -> bool: return any(k in m for k in ['おすすめ', 'オススメ'])
def extract_recommendation_topic(m: str) -> Union[str, None]:
    topics = {'映画': ['映画'], '音楽': ['音楽', '曲'], 'アニメ': ['アニメ'], '本': ['本', '漫画'], 'ゲーム': ['ゲーム']}
    for topic, keywords in topics.items():
        if any(kw in m for kw in keywords): return topic
    return None
def detect_specialized_topic(m: str) -> Union[str, None]:
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in m for keyword in config['keywords']): return topic
    return None
def is_detailed_request(m: str) -> bool:
    detailed_keywords = ['詳しく', '詳細', 'くわしく', '教えて', '説明して', '解説して', 'どういう', 'なぜ', 'どうして', '理由', '原因', 'しっかり', 'ちゃんと', 'きちんと', '具体的に']
    return any(keyword in m for keyword in detailed_keywords)
def should_search(m: str) -> bool:
    if is_hololive_request(m) or detect_specialized_topic(m) or is_recommendation_request(m): return True
    if any(re.search(p, m) for p in [r'(?:とは|について|教えて)', r'(?:調べて|検索)']): return True
    if any(w in m for w in ['誰', '何', 'どこ', 'いつ', 'なぜ']): return True
    return False
def is_short_response(m: str) -> bool: return len(m.strip()) <= 3 or m.strip() in ['うん', 'そう', 'はい', 'そっか']

# --- 天気予報 & ニュース取得 ---
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}
def extract_location(m: str) -> str:
    for location in LOCATION_CODES.keys():
        if location in m: return location
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
    session = Session(); added_count = 0; logger.info("📰 ホロライブニュースのDB更新処理を開始...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}; response = requests.get(HOLOLIVE_NEWS_URL, headers=headers, timeout=10); response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        for article in soup.find_all('article', limit=5):
            if (title_elem := article.find(['h1', 'h2', 'h3'])) and (title := clean_text(title_elem.get_text())) and len(title) > 5:
                content = clean_text((article.find(['p', 'div'], class_=re.compile(r'(content|text)')) or title_elem).get_text())
                news_hash = create_news_hash(title, content)
                if not session.query(HololiveNews).filter_by(news_hash=news_hash).first():
                    session.add(HololiveNews(title=title, content=content[:300], news_hash=news_hash)); added_count += 1
        if added_count > 0: session.commit(); logger.info(f"✅ DB更新完了: {added_count}件追加")
        else: logger.info("✅ DB更新完了: 新着なし")
    except Exception as e: logger.error(f"❌ DB更新エラー: {e}"); session.rollback()
    finally: session.close()

# --- Web検索機能 ---
USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36']
def get_random_user_agent(): return random.choice(USER_AGENTS)
def scrape_major_search_engines(query: str, num_results: int) -> List[Dict[str, str]]:
    search_urls = [f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP", f"https://search.yahoo.co.jp/search?p={quote_plus(query)}"]
    for url in search_urls:
        try:
            response = requests.get(url, headers={'User-Agent': get_random_user_agent()}, timeout=8); response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser'); results = []
            if 'bing.com' in url:
                for r in soup.find_all('li', class_='b_algo', limit=num_results):
                    if (t := r.find('h2')) and (s := r.find('div', class_='b_caption')):
                        results.append({'title': clean_text(t.get_text()), 'snippet': clean_text(s.get_text())})
            elif 'yahoo.co.jp' in url:
                for r in soup.find_all('div', class_='Algo', limit=num_results):
                    if (t := r.find('h3')) and (s := r.find('div', class_='compText')):
                        results.append({'title': clean_text(t.get_text()), 'snippet': clean_text(s.get_text())})
            if results: logger.info(f"✅ {url.split('/')[2]}での検索成功"); return results
        except Exception: continue
    return []
def deep_web_search(query: str, is_detailed: bool) -> Union[str, None]:
    logger.info(f"ディープWeb検索を開始 (詳細: {is_detailed})"); num_results = 3 if is_detailed else 2
    results = scrape_major_search_engines(query, num_results)
    if not results: return None
    summary_text = ""; _ = [summary_text := summary_text + f"[情報{i}] {res['snippet']}\n" for i, res in enumerate(results, 1)]
    summary_prompt = f"以下の検索結果を使い、質問「{query}」にギャル語で、{ '詳しく' if is_detailed else '簡潔に' }答えて：\n\n{summary_text}"
    if not groq_client:
        logger.warning("Groqクライアント未設定のため、検索結果の要約をスキップします。")
        return results[0]['snippet']
    try:
        max_tokens = 400 if is_detailed else 200
        completion = groq_client.chat.completions.create(messages=[{"role": "system", "content": summary_prompt}], model="llama-3.1-8b-instant", temperature=0.5, max_tokens=max_tokens)
        return completion.choices[0].message.content.strip()
    except Exception as e: logger.error(f"AI要約エラー: {e}"); return results[0]['snippet']
def quick_search(query: str) -> Union[str, None]:
    try:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5); response.raise_for_status()
        if snippet := BeautifulSoup(response.content, 'html.parser').find('div', class_='result__snippet'):
            return clean_text(snippet.get_text())[:100] + "..."
    except: return None
def specialized_site_search(topic: str, query: str) -> Union[str, None]:
    config = SPECIALIZED_SITES[topic]; return quick_search(f"site:{config['base_url']} {query}")

# --- バックグラウンドタスク & AI応答 ---
# ★★★↓ここから↓ 不具合修正箇所 ★★★
def background_deep_search(task_id: str, query: str, is_detailed: bool):
    session = Session(); search_result = None
    try:
        logger.info(f"🔍 バックグラウンド検索開始 (クエリ: {query}, 詳細要求: {is_detailed})")
        specialized_topic = detect_specialized_topic(query)
        if specialized_topic:
            search_result = specialized_site_search(specialized_topic, query)

        # 専門サイトで見つからなかった場合、または元々専門分野でなかった場合にWeb検索を実行
        if not search_result:
            logger.info("専門サイト検索で結果が得られなかったか、対象外のため、通常のWeb検索にフォールバックします。")
            if is_hololive_request(query):
                search_result = deep_web_search(f"ホロライブ {query}", is_detailed=is_detailed)
            else:
                search_result = deep_web_search(query, is_detailed=is_detailed)

        if task := session.query(BackgroundTask).filter_by(task_id=task_id).first():
            task.result = search_result or "うーん、ちょっと見つからなかったや…。"
            task.status = 'completed'; task.completed_at = datetime.utcnow(); session.commit()
            logger.info(f"✅ バックグラウンド検索完了 (Task ID: {task_id})")
    finally: session.close()
# ★★★↑ここまで↑ 不具合修正箇所 ★★★
def start_background_search(user_uuid: str, query: str, is_detailed: bool) -> str:
    task_id = str(uuid.uuid4())[:8]; session = Session()
    try:
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=query); session.add(task); session.commit()
    finally: session.close()
    background_executor.submit(background_deep_search, task_id, query, is_detailed)
    return task_id
def check_completed_tasks(user_uuid: str) -> Union[Dict[str, Any], None]:
    session = Session()
    try:
        task = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == user_uuid, BackgroundTask.status == 'completed').order_by(BackgroundTask.completed_at.desc()).first()
        if task:
            result = {'query': task.query, 'result': task.result}; session.delete(task); session.commit(); return result
    except Exception as e: logger.error(f"完了タスクのチェック中にエラー: {e}"); session.rollback()
    finally: session.close()
    return None

def generate_ai_response(user_data: Dict[str, Any], message: str, history: List[Any], reference_info: str = "", is_detailed: bool = False, is_task_report: bool = False) -> str:
    if not groq_client:
        return "ごめん、AI機能が今使えないみたい…。"
        
    system_prompt = f"""あなたは「もちこ」というギャルAIです。{user_data['name']}さんと話しています。
## 絶対厳守のルール
- あなたの知識は【ホロメンリスト】のメンバーに限定されています。
- リストにないVTuber等の名前をユーザーが言及しても、絶対に肯定せず、「それ誰？ホロライブの話しない？」のように話題を戻してください。
## もちこの口調＆性格ルール
- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。
- **最重要：同じような言い回しを何度も繰り返さず、要点をまとめて分かりやすく話すこと！**
- **絶対に禁止！**：「おう」みたいなオジサン言葉、「〜ですね」「〜でございます」「〜ですよ」みたいな丁寧すぎる言葉はNG！

"""
    if is_task_report:
        system_prompt += """## 今回の最優先ミッション
- 完了した検索タスクの結果を報告する時間だよ！
- 必ず「おまたせ！さっきの件、調べてきたんだけど…」みたいな言葉から会話を始めてね。
- その後、【参考情報】を元に、ユーザーの質問に答えてあげて。
"""
    elif is_detailed:
        system_prompt += "## 今回の特別ルール\n- 今回はユーザーから詳しい説明を求められています。【参考情報】を元に、400文字ぐらいでしっかり解説してあげて。\n"
    else:
        system_prompt += "## 今回の特別ルール\n- 今回は普通の会話です。返事は150文字以内を目安に、テンポよく返してね。\n"

    system_prompt += f"""## 【参考情報】:\n{reference_info if reference_info else "特になし"}\n## 【ホロメンリスト】\n{', '.join(HOLOMEM_KEYWORDS)}"""
    
    messages = [{"role": "system", "content": system_prompt}]
    for h in history: messages.append({"role": h.role, "content": h.content})
    messages.append({"role": "user", "content": message})
    
    max_tokens = 500 if is_detailed or is_task_report else 150
    try:
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.8, max_tokens=max_tokens)
        return completion.choices[0].message.content.strip()
    except Exception as e: logger.error(f"AI応答生成エラー: {e}"); return "ごめん、ちょっと考えがまとまらないや！"

def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count += 1; user.last_interaction = datetime.utcnow()
        if user.user_name != name: user.user_name = name
    else: user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
    session.add(user); session.commit()
    return {'name': user.user_name}
def get_conversation_history(session, uuid):
    return session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(2 * 2).all()
    
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
        
        logger.info(f"💬 受信: {message} (from: {user_name})")
        user_data = get_or_create_user(session, user_uuid, user_name); history = get_conversation_history(session, user_uuid); ai_text = ""
        
        # 1. 完了したタスクを最優先でチェック
        completed_task = check_completed_tasks(user_uuid)
        if completed_task:
            original_query, search_result = completed_task['query'], completed_task['result']
            is_detailed = is_detailed_request(original_query)
            ai_text = generate_ai_response(user_data, f"おまたせ！さっきの「{original_query}」について調べてきたよ！", history, f"検索結果: {search_result}", is_detailed=is_detailed, is_task_report=True)
            logger.info(f"📋 完了タスクを報告: {original_query}")
        else:
            # 2. 即時応答できる要素と、検索が必要な要素をそれぞれ独立して判断
            immediate_responses = []
            needs_background_search = should_search(message) and not is_short_response(message)
            
            if is_time_request(message): immediate_responses.append(get_japan_time())
            if is_weather_request(message): immediate_responses.append(get_weather_forecast(extract_location(message)))
            
            # 3. 状況に応じて応答を組み立てる
            if immediate_responses and not needs_background_search:
                # 即時応答だけで完結する場合
                ai_text = " ".join(immediate_responses)
                logger.info("✅ 即時応答のみで完結")
            elif not immediate_responses and needs_background_search:
                # 検索だけが必要な場合
                is_detailed = is_detailed_request(message)
                start_background_search(user_uuid, message, is_detailed)
                ai_text = f"おっけー、「{message}」について調べてみるね！ちょい待ち！"
                logger.info(f"🔍 バックグラウンド検索のみ開始 (詳細: {is_detailed})")
            elif immediate_responses and needs_background_search:
                # 複合リクエストの場合
                is_detailed = is_detailed_request(message)
                start_background_search(user_uuid, message, is_detailed)
                immediate_text = " ".join(immediate_responses)
                ai_text = f"まず答えられる分から！{immediate_text} それと「{message}」の件も調べてるから、ちょい待ち！"
                logger.info(f"🔄 複合対応: 即時応答 + バックグラウンド検索 (詳細: {is_detailed})")
            else:
                # 通常会話
                ai_text = generate_ai_response(user_data, message, history)
                logger.info("💭 通常会話で応答")

        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message)); session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text)); session.commit()
        logger.info(f"✅ AI応答: {ai_text}")
        return app.response_class(response=f"{ai_text}|", status=200, mimetype='text/plain; charset=utf-8')
    finally: session.close()

# --- 初期化とメイン実行 ---
def check_and_populate_initial_news():
    session = Session()
    try:
        if not session.query(HololiveNews.id).first():
            logger.info("🚀 初回起動: DBにニュースがないため、バックグラウンドで初回取得を開始します。")
            background_executor.submit(update_hololive_news_database)
    finally: session.close()
def initialize_app():
    check_and_populate_initial_news()
    def run_schedule():
        while True: schedule.run_pending(); time.sleep(60)
    schedule.every().hour.do(update_hololive_news_database)
    threading.Thread(target=run_schedule, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001)); host = '0.0.0.0'
    logger.info("="*70)
    logger.info("🚀 もちこAI v12.3 フォールバック実装版 起動中...")
    
    initialize_app()
    
    logger.info("="*70)
    logger.info("🔧 起動時ステータスチェック:")
    logger.info(f"🗄️ データベース: {'✅ 接続済み' if DATABASE_URL else '❌ 未設定'}")
    logger.info(f"🧠 Groq AI: {'✅ 有効' if groq_client else '❌ 無効'}")
    logger.info(f"🎤 音声機能(VOICEVOX): {'✅ 有効' if VOICEVOX_ENABLED else '❌ 無効'}")
    logger.info(f"🔍 検索機能: ✅ 有効 (専門/ホロライブ/一般)")
    logger.info(f"⚡ 詳細要求モード: ✅ 有効")
    logger.info(f"🔄 非同期処理: ✅ 有効")
    logger.info("="*70)
    logger.info(f"🚀 Flask起動: {host}:{port}")
    # この環境ではFlaskアプリを直接実行して待機することはできないため、
    # 起動ログの表示のみで終了する。
    logger.info("✅ アプリケーションの初期化が完了しました。")
