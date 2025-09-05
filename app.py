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
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Index, Text, Boolean
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

# 設定定数
VOICEVOX_MAX_TEXT_LENGTH = 30  # 🔧 短縮
VOICEVOX_FAST_TIMEOUT = 3      # 🔧 タイムアウト短縮
CONVERSATION_HISTORY_TURNS = 2
VOICE_DIR = '/tmp/voices'
SERVER_URL = "https://slautofriendbot.onrender.com"

# バックグラウンドタスク管理
background_executor = ThreadPoolExecutor(max_workers=5)
pending_searches = {}  # user_uuid -> search_data
search_lock = Lock()

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name: str) -> Union[str, None]:
    """環境変数または秘密ファイルから設定値を取得"""
    env_value = os.environ.get(name)
    if env_value: 
        return env_value
    try:
        with open(f'/etc/secrets/{name}', 'r') as f:
            return f.read().strip()
    except Exception:
        return None

# 必要な設定値を取得
DATABASE_URL = get_secret('DATABASE_URL')
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
WEATHER_API_KEY = get_secret('WEATHER_API_KEY')

# --- クライアント初期化 ---
groq_client = None
if GROQ_API_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groqクライアント初期化成功")
    except Exception as e:
        logger.error(f"❌ Groqクライアント初期化エラー: {e}")

# VOICEVOX設定
VOICEVOX_URLS = [
    'http://localhost:50021',
    'http://127.0.0.1:50021',
    'http://voicevox-engine:50021',
    'http://voicevox:50021'
]
WORKING_VOICEVOX_URL = VOICEVOX_URL_FROM_ENV or VOICEVOX_URLS[0]
VOICEVOX_ENABLED = True

# 必須設定チェック
if not all([DATABASE_URL, groq_client]):
    logger.critical("FATAL: 必須設定(DATABASE_URL or GROQ_API_KEY)が不足しています。")
    sys.exit(1)

# Flask & データベース初期化
app = Flask(__name__)
CORS(app)
engine = create_engine(DATABASE_URL)
Base = declarative_base()

# --- データベースモデル ---
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
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

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000))
    published_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    news_hash = Column(String(100), unique=True)  # 重複防止用ハッシュ

class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False)
    task_type = Column(String(50), nullable=False)  # 'search', 'voice'
    query = Column(Text, nullable=False)
    result = Column(Text)
    status = Column(String(20), default='pending')  # pending, completed, failed
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime)

# テーブル作成
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# --- ホロライブ関連設定 ---
HOLOLIVE_NEWS_URL = "https://hololive.hololivepro.com/news"
HOLOLIVE_WIKI_BASE = "https://seesaawiki.jp/hololivetv/"

# ホロライブメンバーリスト（拡張版）
HOLOMEM_KEYWORDS = [
    # 基本キーワード
    'ホロライブ', 'ホロメン', 'hololive', 'VTuber', 'バーチャル',
    
    # ホロライブ0期生
    'ときのそら', 'ロボ子', 'さくらみこ', '星街すいせい', 'AZKi',
    
    # 1期生
    '白上フブキ', '夏色まつり',
    
    # 2期生
    '湊あくあ', '紫咲シオン', '百鬼あやめ', '大空スバル', '大神ミオ',
    
    # ゲーマーズ
    '猫又おかゆ', '戌神ころね',
    
    # 3期生
    '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン',
    
    # 4期生
    '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ',
    
    # 5期生
    '雪花ラミィ', '尾丸ポルカ', '桃鈴ねね', '獅白ぼたん',
    
    # 6期生
    'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは',
    
    # ホロライブEN
    '森美声', 'カリオペ', 'ワトソン', 'アメリア', 'がうる・ぐら',
    
    # その他関連用語
    'ホロライブプロダクション', 'カバー株式会社', 'YAGOO', '谷郷元昭',
    'ホロフェス', 'ホロライブエラー', 'ホロライブオルタナティブ'
]

# --- ユーティリティ関数 ---
def clean_text(text: str) -> str:
    """HTMLタグを除去し、テキストをクリーンアップ"""
    if not text:
        return ""
    # HTMLタグ除去
    text = re.sub(r'<[^>]+>', '', text)
    # 複数の空白を単一に
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_japan_time() -> str:
    """現在の日本時間を取得"""
    from datetime import timezone, timedelta
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    weekdays = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日']
    weekday = weekdays[now.weekday()]
    return f"今は{now.year}年{now.month}月{now.day}日({weekday})の{now.hour}時{now.minute}分だよ！"

def create_news_hash(title: str, content: str) -> str:
    """ニュースのハッシュ値を生成（重複チェック用）"""
    import hashlib
    combined = f"{title}{content[:100]}"
    return hashlib.md5(combined.encode('utf-8')).hexdigest()

# --- 判定関数 ---
def is_time_request(message: str) -> bool:
    """時刻に関する質問かどうか判定"""
    time_keywords = ['今何時', '時間', '時刻', 'いま何時', '現在時刻', '今の時間']
    return any(keyword in message for keyword in time_keywords)

def is_weather_request(message: str) -> bool:
    """天気に関する質問かどうか判定"""
    return any(keyword in message for keyword in ['天気', 'てんき', '気温', '降水確率', '晴れ', '雨', '曇り'])

def is_recommendation_request(message: str) -> bool:
    """おすすめに関する質問かどうか判定"""
    return any(keyword in message for keyword in ['おすすめ', 'オススメ', '人気', '流行', 'はやり', 'ランキング'])

def is_hololive_request(message: str) -> bool:
    """ホロライブに関する質問かどうか判定"""
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def should_search(message: str) -> bool:
    """Web検索が必要かどうか判定"""
    search_patterns = [
        r'(?:とは|について|教えて|知りたい)',
        r'(?:最新|今日|ニュース)',
        r'(?:調べて|検索)'
    ]
    search_words = ['誰', '何', 'どこ', 'いつ', 'どうして', 'なぜ']
    
    return (any(re.search(pattern, message) for pattern in search_patterns) or
            any(word in message for word in search_words))

def is_short_response(message: str) -> bool:
    """短い相槌かどうか判定"""
    short_responses = ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'へー', 'ふーん', 'おお', 'わかった']
    return len(message.strip()) <= 3 or message.strip() in short_responses

# --- 天気取得機能 ---
LOCATION_CODES = {
    "東京": "130000",
    "大阪": "270000", 
    "名古屋": "230000",
    "福岡": "400000",
    "札幌": "016000",
    "仙台": "040000",
    "広島": "340000"
}

def extract_location(message: str) -> str:
    """メッセージから地域を抽出"""
    for location in LOCATION_CODES.keys():
        if location in message:
            return location
    return "東京"  # デフォルト

def get_weather_forecast(location: str) -> Union[str, None]:
    """気象庁APIから天気予報を取得"""
    area_code = LOCATION_CODES.get(location)
    if not area_code:
        return None
        
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        weather_text = clean_text(data.get('text', ''))
        office = data.get('publishingOffice', '気象庁')
        
        return f"{location}の天気だよ！{weather_text[:50]}..."  # 🔧 短縮
    except Exception as e:
        logger.error(f"天気API取得エラー: {e}")
        return None

# --- ホロライブ情報取得機能 ---
def scrape_hololive_news() -> List[Dict[str, str]]:
    """ホロライブ通信から最新ニュースを取得"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(HOLOLIVE_NEWS_URL, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        news_items = []
        
        # ニュース項目を取得（サイト構造に応じて調整が必要）
        articles = soup.find_all('article', limit=10)  # 最新10件
        
        for article in articles:
            try:
                title_elem = article.find(['h1', 'h2', 'h3', 'h4'])
                if title_elem:
                    title = clean_text(title_elem.get_text())
                    
                    # 本文取得
                    content_elem = article.find(['p', 'div'], class_=re.compile(r'(content|text|description)'))
                    content = clean_text(content_elem.get_text()) if content_elem else title
                    
                    # URLを取得
                    link_elem = article.find('a')
                    url = link_elem.get('href') if link_elem else None
                    if url and url.startswith('/'):
                        url = urljoin(HOLOLIVE_NEWS_URL, url)
                    
                    if title and len(title) > 5:  # 短すぎるタイトルは除外
                        news_items.append({
                            'title': title,
                            'content': content[:500],  # 500文字まで
                            'url': url,
                            'published_date': datetime.utcnow()
                        })
                        
            except Exception as e:
                logger.error(f"記事解析エラー: {e}")
                continue
        
        logger.info(f"✅ ホロライブニュース取得: {len(news_items)}件")
        return news_items
        
    except Exception as e:
        logger.error(f"ホロライブニュース取得エラー: {e}")
        return []

def update_hololive_news_database():
    """ホロライブニュースをデータベースに更新"""
    session = Session()
    try:
        news_items = scrape_hololive_news()
        added_count = 0
        
        for item in news_items:
            # 重複チェック用ハッシュ
            news_hash = create_news_hash(item['title'], item['content'])
            
            # 既存チェック
            existing = session.query(HololiveNews).filter_by(news_hash=news_hash).first()
            if existing:
                continue
            
            # 新規追加
            news = HololiveNews(
                title=item['title'],
                content=item['content'],
                url=item['url'],
                published_date=item['published_date'],
                news_hash=news_hash,
                created_at=datetime.utcnow()
            )
            session.add(news)
            added_count += 1
        
        session.commit()
        
        if added_count > 0:
            logger.info(f"📰 ホロライブニュース更新: {added_count}件追加")
        else:
            logger.info("📰 ホロライブニュース: 新着なし")
            
    except Exception as e:
        logger.error(f"ホロライブニュースDB更新エラー: {e}")
        session.rollback()
    finally:
        session.close()

def get_hololive_info_from_db(query: str = "") -> Union[str, None]:
    """データベースから最新のホロライブ情報を取得"""
    session = Session()
    try:
        # クエリに応じて検索
        if query:
            # キーワード検索
            news_list = session.query(HololiveNews).filter(
                HololiveNews.title.contains(query) | HololiveNews.content.contains(query)
            ).order_by(HololiveNews.created_at.desc()).limit(3).all()
        else:
            # 最新情報
            news_list = session.query(HololiveNews)\
                .order_by(HololiveNews.created_at.desc())\
                .limit(3).all()
        
        if news_list:
            result = "最新のホロライブ情報だよ！\n"
            for news in news_list:
                result += f"・{news.title}: {news.content[:80]}...\n"
            return result[:200] + "..."  # 短縮
            
        return None
        
    except Exception as e:
        logger.error(f"ホロライブDB取得エラー: {e}")
        return None
    finally:
        session.close()

# --- 無料検索エンジン設定 ---
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
]

def get_random_user_agent():
    """ランダムなUser-Agentを取得"""
    return random.choice(USER_AGENTS)

# --- ホロライブ専用検索機能 ---
def search_hololive_wiki(query: str) -> Union[str, None]:
    """Seesaa Wikiでホロライブ情報を検索"""
    try:
        # 検索URL構築
        search_url = f"{HOLOLIVE_WIKI_BASE}d/search?keywords={quote_plus(query)}"
        headers = {'User-Agent': get_random_user_agent()}
        
        response = requests.get(search_url, headers=headers, timeout=8)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 検索結果から最初の項目を取得
        result_items = soup.find_all(['div', 'article'], class_=re.compile(r'(result|search|content)'))
        
        for item in result_items[:3]:  # 上位3件まで
            text_content = clean_text(item.get_text())
            if text_content and len(text_content) > 20:
                return text_content[:150] + "..."
        
        # 検索結果がない場合は、メインページから情報取得を試行
        main_response = requests.get(HOLOLIVE_WIKI_BASE, headers=headers, timeout=8)
        main_soup = BeautifulSoup(main_response.content, 'html.parser')
        
        content_divs = main_soup.find_all(['div', 'section'], limit=5)
        for div in content_divs:
            text = clean_text(div.get_text())
            if query in text and len(text) > 30:
                return text[:150] + "..."
        
        return None
        
    except Exception as e:
        logger.error(f"ホロライブWiki検索エラー: {e}")
        return None

# --- 軽量検索実装 ---
def quick_search(query: str) -> Union[str, None]:
    """高速軽量検索（簡易版）"""
    try:
        # ホロライブ関連の場合は専用検索
        if is_hololive_request(query):
            wiki_result = search_hololive_wiki(query)
            if wiki_result:
                return wiki_result
        
        # 最もシンプルなDuckDuckGo検索
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {'User-Agent': get_random_user_agent()}
        
        response = requests.get(url, headers=headers, timeout=5)  # タイムアウト短縮
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 最初の結果だけ取得
        result_div = soup.find('div', class_='links_main')
        if result_div:
            snippet_elem = result_div.find('div', class_='result__snippet')
            if snippet_elem:
                snippet = clean_text(snippet_elem.get_text())
                return snippet[:100] + "..." if len(snippet) > 100 else snippet  # 短縮
        
        return None
        
    except Exception as e:
        logger.error(f"軽量検索エラー: {e}")
        return None

# --- ディープ検索実装 ---
def deep_search(query: str) -> Union[str, None]:
    """ディープ検索（複数ソース）"""
    try:
        results = []
        
        # 1. ホロライブWiki検索（ホロライブ関連の場合）
        if is_hololive_request(query):
            wiki_result = search_hololive_wiki(query)
            if wiki_result:
                results.append(f"Wiki情報: {wiki_result}")
        
        # 2. 通常のWeb検索
        general_result = quick_search(query)
        if general_result:
            results.append(f"Web情報: {general_result}")
        
        # 3. データベースからホロライブ情報
        if is_hololive_request(query):
            db_result = get_hololive_info_from_db(query)
            if db_result:
                results.append(f"最新情報: {db_result[:100]}...")
        
        if results:
            return " / ".join(results[:2])  # 最大2つまで結合
        
        return None
        
    except Exception as e:
        logger.error(f"ディープ検索エラー: {e}")
        return None

# --- バックグラウンド検索システム ---
def background_deep_search(task_id: str, user_uuid: str, query: str):
    """バックグラウンドでディープ検索実行"""
    session = Session()
    try:
        logger.info(f"🔍 バックグラウンド検索開始: {query}")
        
        # ホロライブ関連かチェック
        if is_hololive_request(query):
            # 1. まずWiki検索
            wiki_result = search_hololive_wiki(query)
            if not wiki_result:
                # 2. Wiki検索できなかったらディープ検索
                search_result = deep_search(query)
            else:
                search_result = wiki_result
        else:
            # 通常のクイック検索
            search_result = quick_search(query)
        
        if search_result and groq_client:
            # AIで要約
            try:
                completion = groq_client.chat.completions.create(
                    messages=[{
                        "role": "system", 
                        "content": f"以下の情報を「{query}」について30字以内で簡潔にまとめて：{search_result}"
                    }],
                    model="llama-3.1-8b-instant",
                    temperature=0.2,
                    max_tokens=50
                )
                search_result = completion.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"AI要約エラー: {e}")
        
        # 結果をデータベースに保存
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = search_result or "検索結果が見つからなかったよ..."
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
            
            logger.info(f"✅ バックグラウンド検索完了: {task_id}")
        
    except Exception as e:
        logger.error(f"バックグラウンド検索エラー: {e}")
        # エラー状態を記録
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.status = 'failed'
            task.completed_at = datetime.utcnow()
            session.commit()
    finally:
        session.close()

def start_background_search(user_uuid: str, query: str) -> str:
    """バックグラウンド検索を開始"""
    task_id = str(uuid.uuid4())[:8]
    
    # データベースにタスク記録
    session = Session()
    try:
        task = BackgroundTask(
            task_id=task_id,
            user_uuid=user_uuid,
            task_type='search',
            query=query,
            status='pending'
        )
        session.add(task)
        session.commit()
    finally:
        session.close()
    
    # バックグラウンドで実行
    background_executor.submit(background_deep_search, task_id, user_uuid, query)
    
    return task_id

# --- 音声生成システム ---
def background_voice_generation(task_id: str, user_uuid: str, text: str):
    """バックグラウンドで音声生成"""
    session = Session()
    try:
        if not VOICEVOX_ENABLED:
            return
            
        logger.info(f"🔊 バックグラウンド音声生成開始: {text[:20]}...")
        
        # テキストを短縮
        if len(text) > VOICEVOX_MAX_TEXT_LENGTH:
            text = text[:VOICEVOX_MAX_TEXT_LENGTH] + "..."
        
        # 音声合成
        query_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": 1},
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        query_response.raise_for_status()
        
        synthesis_response = requests.post(
            f"{WORKING_VOICEVOX_URL}/synthesis",
            params={"speaker": 1},
            json=query_response.json(),
            timeout=VOICEVOX_FAST_TIMEOUT
        )
        synthesis_response.raise_for_status()
        
        # ファイル保存
        filename = f"voice_{task_id}_{int(time.time())}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(synthesis_response.content)
        
        # 結果をデータベースに保存
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = f"/voice/{filename}"
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
            
        logger.info(f"✅ バックグラウンド音声生成完了: {filename}")
        
    except Exception as e:
        logger.error(f"バックグラウンド音声生成エラー: {e}")
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.status = 'failed'
            task.completed_at = datetime.utcnow()
            session.commit()
    finally:
        session.close()

def start_background_voice(user_uuid: str, text: str) -> str:
    """バックグラウンド音声生成を開始"""
    task_id = str(uuid.uuid4())[:8]
    
    # データベースにタスク記録
    session = Session()
    try:
        task = BackgroundTask(
            task_id=task_id,
            user_uuid=user_uuid,
            task_type='voice',
            query=text,
            status='pending'
        )
        session.add(task)
        session.commit()
    finally:
        session.close()
    
    # バックグラウンドで実行
    background_executor.submit(background_voice_generation, task_id, user_uuid, text)
    
    return task_id

# --- 完了したタスクをチェック ---
def check_completed_tasks(user_uuid: str) -> Dict[str, Any]:
    """完了したバックグラウンドタスクをチェック"""
    session = Session()
    try:
        # 完了した検索タスク
        completed_searches = session.query(BackgroundTask).filter(
            BackgroundTask.user_uuid == user_uuid,
            BackgroundTask.task_type == 'search',
            BackgroundTask.status == 'completed',
            BackgroundTask.completed_at > datetime.utcnow() - timedelta(minutes=5)  # 5分以内
        ).order_by(BackgroundTask.completed_at.desc()).first()
        
        # 完了した音声タスク
        completed_voice = session.query(BackgroundTask).filter(
            BackgroundTask.user_uuid == user_uuid,
            BackgroundTask.task_type == 'voice',
            BackgroundTask.status == 'completed',
            BackgroundTask.completed_at > datetime.utcnow() - timedelta(minutes=2)  # 2分以内
        ).order_by(BackgroundTask.completed_at.desc()).first()
        
        result = {}
        if completed_searches:
            result['search'] = {
                'query': completed_searches.query,
                'result': completed_searches.result
            }
            # タスクを削除（一度だけ通知）
            session.delete(completed_searches)
            
        if completed_voice:
            result['voice'] = completed_voice.result
            session.delete(completed_voice)
            
        session.commit()
        return result
        
    except Exception as e:
        logger.error(f"タスクチェックエラー: {e}")
        return {}
    finally:
        session.close()

# --- 高速AI応答生成 ---
def generate_quick_ai_response(user_data: Dict[str, Any], message: str, history: List[Any], 
                             completed_tasks: Dict[str, Any] = None) -> str:
    """高速AI応答生成（即座にレスポンス）"""
    if not groq_client:
        return "今ちょっと調子悪いから、また話しかけて！"
    
    # 即座に応答できる情報
    immediate_info = ""
    
    # 時刻要求
    if is_time_request(message):
        immediate_info = get_japan_time()
    
    # 天気要求
    elif is_weather_request(message):
        location = extract_location(message)
        weather_info = get_weather_forecast(location)
        if weather_info:
            immediate_info = weather_info
    
    # ホロライブ情報要求
    elif is_hololive_request(message):
        holo_info = get_hololive_info_from_db(message)
        if holo_info:
            immediate_info = holo_info
    
    # 完了したバックグラウンドタスクの結果
    background_update = ""
    if completed_tasks and completed_tasks.get('search'):
        search_data = completed_tasks['search']
        background_update = f"そういえばさ、さっきの「{search_data['query']}」の話なんだけど、{search_data['result']}"
    
    # 短いシステムプロンプト
    system_prompt = f"""あなたは「もちこ」というギャルAIです。{user_data['name']}と話してます。

# ルール:
1. 自分は「あてぃし」、語尾に「〜じゃん」「〜だし」「〜的な？」使用
2. 「まじ」「てか」「やばい」「うける」使用  
3. **返答は30字以内で短く！**
4. 丁寧語禁止

# 情報:
{immediate_info}

# バックグラウンド完了情報:
{background_update}
"""
    
    messages = [{"role": "system", "content": system_prompt}]
    
    # 最新の会話履歴のみ
    for h in history[-2:]:
        messages.append({"role": h.role, "content": h.content})
    
    messages.append({"role": "user", "content": message})

    try:
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            temperature=0.8,
            max_tokens=60  # 🔧 大幅短縮
        )
        return completion.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"AI応答生成エラー: {e}")
        return "ちょっと考え中...またすぐ話しかけて！"

# --- ユーザー管理機能 ---
def get_or_create_user(session, uuid, name):
    """ユーザー情報を取得または作成"""
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name:
            user.user_name = name
    else:
        user = UserMemory(
            user_uuid=uuid,
            user_name=name,
            interaction_count=1,
            last_interaction=datetime.utcnow()
        )
    
    session.add(user)
    session.commit()
    
    return {
        'uuid': user.user_uuid,
        'name': user.user_name,
        'interaction_count': user.interaction_count
    }

def get_conversation_history(session, uuid, turns=CONVERSATION_HISTORY_TURNS):
    """会話履歴を取得"""
    histories = session.query(ConversationHistory)\
        .filter_by(user_uuid=uuid)\
        .order_by(ConversationHistory.timestamp.desc())\
        .limit(turns * 2)\
        .all()
    
    return list(reversed(histories))

# --- VOICEVOX初期化 ---
def check_voicevox_connection():
    """VOICEVOX接続をチェック"""
    global WORKING_VOICEVOX_URL, VOICEVOX_ENABLED
    
    for url in VOICEVOX_URLS:
        try:
            response = requests.get(f"{url}/version", timeout=2)
            if response.status_code == 200:
                WORKING_VOICEVOX_URL = url
                logger.info(f"✅ VOICEVOX接続成功: {url}")
                return True
        except:
            continue
    
    logger.warning("⚠️ VOICEVOX無効化")
    VOICEVOX_ENABLED = False
    return False

def initialize_voice_directory():
    """音声ディレクトリを初期化"""
    global VOICE_DIR, VOICEVOX_ENABLED
    
    try:
        os.makedirs(VOICE_DIR, exist_ok=True)
        test_file = os.path.join(VOICE_DIR, 'test.tmp')
        with open(test_file, 'w') as f:
            f.write('test')
        os.remove(test_file)
        logger.info(f"✅ 音声ディレクトリ準備完了: {VOICE_DIR}")
    except Exception as e:
        logger.error(f"音声ディレクトリエラー: {e}")
        VOICEVOX_ENABLED = False

# --- ホロライブ情報の定期更新スケジューリング ---
def schedule_hololive_news_updates():
    """ホロライブニュースの定期更新をスケジュール"""
    def update_task():
        logger.info("📰 ホロライブニュース定期更新開始")
        update_hololive_news_database()
    
    # 1時間毎に更新
    schedule.every().hour.do(update_task)
    
    # 初回実行
    update_task()
    
    # スケジュール実行スレッド
    def run_schedule():
        while True:
            schedule.run_pending()
            time.sleep(60)  # 1分毎にスケジュールチェック
    
    threading.Thread(target=run_schedule, daemon=True).start()
    logger.info("📅 ホロライブニュース定期更新スケジュール開始（1時間毎）")

# --- Flask エンドポイント ---
@app.route('/health', methods=['GET'])
def health_check():
    """ヘルスチェック"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'services': {
            'database': 'ok' if DATABASE_URL else 'error',
            'groq': 'ok' if groq_client else 'error',
            'voicevox': 'ok' if VOICEVOX_ENABLED else 'disabled',
            'background_tasks': 'ok',
            'hololive_news': 'ok'
        }
    })

@app.route('/voice/<filename>')
def serve_voice_file(filename):
    """音声ファイル配信"""
    try:
        return send_from_directory(VOICE_DIR, filename)
    except Exception as e:
        logger.error(f"音声ファイル配信エラー: {e}")
        return "File not found", 404

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """🚀 超高速チャットエンドポイント"""
    session = Session()
    try:
        data = request.json
        if not data:
            return "Error: JSON required", 400
            
        user_uuid = data.get('uuid')
        user_name = data.get('name') 
        message = data.get('message', '').strip()
        
        if not all([user_uuid, user_name, message]):
            return "Error: uuid, name, message required", 400
        
        logger.info(f"💬 受信: {user_name} ({user_uuid[:8]}...): {message}")
        
        # ユーザー情報取得
        user_data = get_or_create_user(session, user_uuid, user_name)
        
        # 会話履歴取得
        history = get_conversation_history(session, user_uuid)
        
        # 🔍 完了したバックグラウンドタスクをチェック
        completed_tasks = check_completed_tasks(user_uuid)
        
        # 🚀 即座にAI応答生成
        ai_text = generate_quick_ai_response(user_data, message, history, completed_tasks)
        
        # 🔍 検索が必要かチェック（バックグラウンドで実行）
        search_task_id = None
        if should_search(message) and not is_short_response(message):
            search_task_id = start_background_search(user_uuid, message)
            logger.info(f"🔍 バックグラウンド検索開始: {search_task_id}")
        
        # 🔊 音声生成開始（バックグラウンド）
        voice_task_id = None
        if VOICEVOX_ENABLED:
            voice_task_id = start_background_voice(user_uuid, ai_text)
        
        # 会話履歴保存
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()
        
        # 🎵 完了した音声があるかチェック
        audio_url = ""
        if completed_tasks.get('voice'):
            audio_url = completed_tasks['voice']
        
        # レスポンス形式: "AI応答テキスト|音声URL"
        response_text = f"{ai_text}|{audio_url}"
        
        logger.info(f"💭 AI応答: {ai_text}")
        if search_task_id:
            logger.info(f"🔍 検索タスクID: {search_task_id}")
        if voice_task_id:
            logger.info(f"🔊 音声タスクID: {voice_task_id}")
        if audio_url:
            logger.info(f"🎵 音声URL: {audio_url}")
        
        return app.response_class(
            response=response_text,
            status=200,
            mimetype='text/plain; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"チャットエラー: {e}")
        return "Internal server error", 500
    finally:
        session.close()

@app.route('/api/status', methods=['GET'])
def api_status():
    """API状態確認"""
    session = Session()
    try:
        user_count = session.query(UserMemory).count()
        conversation_count = session.query(ConversationHistory).count()
        pending_tasks = session.query(BackgroundTask).filter_by(status='pending').count()
        hololive_news_count = session.query(HololiveNews).count()
        
        return jsonify({
            'server_url': SERVER_URL,
            'status': 'active',
            'users': user_count,
            'conversations': conversation_count,
            'pending_background_tasks': pending_tasks,
            'hololive_news_count': hololive_news_count,
            'voicevox': VOICEVOX_ENABLED,
            'fast_response': True,
            'version': '3.1.0-hololive-enhanced'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/tasks/<user_uuid>', methods=['GET'])
def get_user_tasks(user_uuid):
    """ユーザーのバックグラウンドタスク状況を確認"""
    session = Session()
    try:
        tasks = session.query(BackgroundTask).filter(
            BackgroundTask.user_uuid == user_uuid,
            BackgroundTask.created_at > datetime.utcnow() - timedelta(hours=1)
        ).all()
        
        result = []
        for task in tasks:
            result.append({
                'task_id': task.task_id,
                'type': task.task_type,
                'query': task.query,
                'status': task.status,
                'result': task.result if task.status == 'completed' else None,
                'created_at': task.created_at.isoformat(),
                'completed_at': task.completed_at.isoformat() if task.completed_at else None
            })
        
        return jsonify({'tasks': result})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/search_test', methods=['GET'])
def search_test():
    """検索機能テスト"""
    try:
        test_query = request.args.get('q', 'ホロライブ')
        
        # ホロライブ関連かチェック
        if is_hololive_request(test_query):
            # Wiki検索テスト
            wiki_result = search_hololive_wiki(test_query)
            # DB検索テスト
            db_result = get_hololive_info_from_db(test_query)
            
            return jsonify({
                'query': test_query,
                'is_hololive': True,
                'wiki_result': wiki_result,
                'db_result': db_result,
                'type': 'hololive_search'
            })
        else:
            # 通常検索テスト
            result = quick_search(test_query)
            return jsonify({
                'query': test_query,
                'is_hololive': False,
                'result': result,
                'type': 'quick_search'
            })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/hololive/news', methods=['GET'])
def get_hololive_news():
    """ホロライブニュース一覧取得"""
    session = Session()
    try:
        limit = int(request.args.get('limit', 10))
        
        news_list = session.query(HololiveNews)\
            .order_by(HololiveNews.created_at.desc())\
            .limit(limit)\
            .all()
        
        result = []
        for news in news_list:
            result.append({
                'title': news.title,
                'content': news.content[:200],
                'url': news.url,
                'published_date': news.published_date.isoformat() if news.published_date else None,
                'created_at': news.created_at.isoformat()
            })
        
        return jsonify({
            'news': result,
            'count': len(result)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@app.route('/api/hololive/update', methods=['POST'])
def manual_hololive_update():
    """ホロライブニュース手動更新"""
    try:
        update_hololive_news_database()
        return jsonify({'status': 'success', 'message': 'ホロライブニュース更新完了'})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500

# --- バックグラウンドタスク管理 ---
def cleanup_old_tasks():
    """古いタスクをクリーンアップ"""
    session = Session()
    try:
        # 24時間以前のタスクを削除
        day_ago = datetime.utcnow() - timedelta(hours=24)
        deleted_tasks = session.query(BackgroundTask)\
            .filter(BackgroundTask.created_at < day_ago)\
            .delete()
        
        # 古い会話履歴も削除（7日以前）
        week_ago = datetime.utcnow() - timedelta(days=7)
        deleted_conversations = session.query(ConversationHistory)\
            .filter(ConversationHistory.timestamp < week_ago)\
            .delete()
        
        # 古いホロライブニュースも削除（30日以前）
        month_ago = datetime.utcnow() - timedelta(days=30)
        deleted_news = session.query(HololiveNews)\
            .filter(HololiveNews.created_at < month_ago)\
            .delete()
        
        session.commit()
        
        if deleted_tasks > 0 or deleted_conversations > 0 or deleted_news > 0:
            logger.info(f"🧹 クリーンアップ完了: タスク{deleted_tasks}件、会話{deleted_conversations}件、ニュース{deleted_news}件削除")
            
    except Exception as e:
        logger.error(f"クリーンアップエラー: {e}")
        session.rollback()
    finally:
        session.close()

def start_background_tasks():
    """バックグラウンドタスク開始"""
    def periodic_cleanup():
        while True:
            try:
                time.sleep(1800)  # 30分毎
                cleanup_old_tasks()
            except Exception as e:
                logger.error(f"定期クリーンアップエラー: {e}")
    
    threading.Thread(target=periodic_cleanup, daemon=True).start()
    logger.info("🚀 バックグラウンドタスク開始")

# --- メイン実行 ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0'
    
    logger.info("=" * 70)
    logger.info("🚀 もちこAI ホロライブ強化版 起動中...")
    logger.info(f"🌐 サーバーURL: {SERVER_URL}")
    logger.info("=" * 70)
    
    # 初期化
    initialize_voice_directory()
    if VOICEVOX_ENABLED:
        check_voicevox_connection()
    
    # バックグラウンドタスク開始
    start_background_tasks()
    
    # ホロライブニュース定期更新開始
    schedule_hololive_news_updates()
    
    # 検索機能テスト
    logger.info("🔍 検索エンジンテスト中...")
    test_result = quick_search("test")
    if test_result:
        logger.info("✅ 通常検索: 動作確認")
    else:
        logger.warning("⚠️ 通常検索: 応答なし")
    
    # ホロライブWiki検索テスト
    holo_test = search_hololive_wiki("ホロライブ")
    if holo_test:
        logger.info("✅ ホロライブWiki検索: 動作確認")
    else:
        logger.warning("⚠️ ホロライブWiki検索: 応答なし")
    
    # 起動情報
    logger.info(f"🚀 Flask起動: {host}:{port}")
    logger.info(f"🗄️ データベース: {'✅' if DATABASE_URL else '❌'}")
    logger.info(f"🧠 Groq AI: {'✅' if groq_client else '❌'}")
    logger.info(f"🎤 VOICEVOX: {'✅' if VOICEVOX_ENABLED else '❌'}")
    logger.info(f"📰 ホロライブニュース: ✅ 1時間毎自動更新")
    logger.info(f"🔍 ホロライブWiki検索: ✅ 有効")
    logger.info(f"⚡ 高速レスポンス: ✅ 有効")
    logger.info(f"🔄 バックグラウンド処理: ✅ 有効")
    logger.info(f"💰 検索コスト: 完全無料")
    logger.info("=" * 70)
    
    # Flask起動
    app.run(host=host, port=port, debug=False, threaded=True)
