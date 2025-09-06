import sys
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

# ★ 修正1: ボイスディレクトリを安全に作成
VOICE_DIR = '/tmp/voices'
try:
    os.makedirs(VOICE_DIR, exist_ok=True)
    logger.info(f"✅ Voice directory created: {VOICE_DIR}")
except Exception as e:
    logger.warning(f"⚠️ Voice directory creation failed: {e}")
    VOICE_DIR = '/tmp'  # フォールバック

SERVER_URL = "https://slautofriendbot.onrender.com"
background_executor = ThreadPoolExecutor(max_workers=5)

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name: str) -> Union[str, None]:
    """環境変数から秘密情報を取得"""
    env_value = os.environ.get(name)
    if env_value: 
        return env_value
    return None

# ★★★ローカル実行のためのダミー設定★★★
DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY') or 'DUMMY_GROQ_KEY'
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# --- クライアント初期化 & 必須設定チェック ---
try:
    from groq import Groq
    if GROQ_API_KEY != 'DUMMY_GROQ_KEY':
        groq_client = Groq(api_key=GROQ_API_KEY)
        logger.info("✅ Groq client initialized successfully")
    else:
        groq_client = None
        logger.warning("⚠️ Using dummy Groq key - AI features disabled")
except Exception as e: 
    groq_client = None
    logger.error(f"❌ Groq client initialization failed: {e}")

if not all([DATABASE_URL]): 
    logger.critical("FATAL: データベースURLが不足しています。")
    sys.exit(1)

if not groq_client:
    logger.warning("警告: Groq APIキーが設定されていないため、AI機能は無効です。")

VOICEVOX_ENABLED = True

# --- Flask & データベース初期化 ---
app = Flask(__name__)
CORS(app)

# ★ 修正2: データベース接続をより堅牢に
try:
    engine = create_engine(
        DATABASE_URL, 
        pool_pre_ping=True, 
        pool_recycle=300,
        connect_args={'check_same_thread': False} if 'sqlite' in DATABASE_URL else {}
    )
    logger.info("✅ Database engine created successfully")
except Exception as e:
    logger.error(f"❌ Database engine creation failed: {e}")
    raise

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
    news_hash = Column(String(100), unique=True)

class BackgroundTask(Base): 
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False)
    task_type = Column(String(50), nullable=False)
    query = Column(Text, nullable=False)
    result = Column(Text)
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime)

# ★ 修正3: データベース初期化をtry-catchで囲む
try:
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info("✅ Database tables created successfully")
except Exception as e:
    logger.error(f"❌ Database table creation failed: {e}")
    raise

def add_news_hash_column_if_not_exists(engine):
    """news_hashカラムが存在しない場合は追加"""
    try:
        inspector = inspect(engine)
        columns = [col['name'] for col in inspector.get_columns('hololive_news')]
        if 'news_hash' not in columns:
            with engine.connect() as con:
                trans = con.begin()
                try: 
                    con.execute(text("ALTER TABLE hololive_news ADD COLUMN news_hash VARCHAR(100) UNIQUE;"))
                    trans.commit()
                    logger.info("✅ news_hash column added successfully")
                except Exception as e: 
                    trans.rollback()
                    logger.warning(f"⚠️ news_hash column add failed: {e}")
        else:
            logger.info("✅ news_hash column already exists")
    except Exception as e:
        logger.warning(f"⚠️ Column check failed: {e}")

add_news_hash_column_if_not_exists(engine)

# --- 専門サイト & ホロライブ設定 ---
SPECIALIZED_SITES = {
    'Blender': {
        'base_url': 'https://docs.blender.org/manual/ja/latest/', 
        'keywords': ['Blender', 'ブレンダー']
    },
    'CGニュース': {
        'base_url': 'https://modelinghappy.com/', 
        'keywords': ['CGニュース', '3DCG']
    },
    '脳科学・心理学': {
        'base_url': 'https://nazology.kusuguru.co.jp/', 
        'keywords': ['脳科学', '心理学']
    },
    'セカンドライフ': {
        'base_url': 'https://community.secondlife.com/news/', 
        'keywords': ['セカンドライフ', 'Second Life', 'SL']
    }
}

HOLOLIVE_NEWS_URL = "https://hololive.hololivepro.com/news"
HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル', 
    'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ', 
    '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ', 
    '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた', 
    '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', 
    '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', 
    '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 
    'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ', 
    'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト', 
    'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス', 'ムーナ・ホシノヴァ', 
    'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ', 
    'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん', 
    '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO'
]

# --- ユーティリティ & 判定関数 ---
def clean_text(text: str) -> str: 
    """HTMLタグや余分な空白を除去"""
    if not text:
        return ""
    # HTMLタグを除去
    text = re.sub(r'<[^>]+>', '', text)
    # 連続する空白を1つにまとめ
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def get_japan_time() -> str: 
    """日本時間を取得して文字列で返す"""
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    return f"今は{now.year}年{now.month}月{now.day}日の{now.hour}時{now.minute}分だよ！"

def create_news_hash(title: str, content: str) -> str: 
    """ニュースのハッシュ値を生成（重複チェック用）"""
    hash_string = f"{title}{content[:100]}"
    return hashlib.md5(hash_string.encode('utf-8')).hexdigest()

def is_time_request(message: str) -> bool: 
    """時間に関する質問かどうか判定"""
    time_keywords = ['今何時', '時間', '時刻', '何時', 'なんじ']
    return any(keyword in message for keyword in time_keywords)

def is_weather_request(message: str) -> bool: 
    """天気に関する質問かどうか判定"""
    weather_keywords = ['天気', 'てんき', '気温', '雨', '晴れ', '曇り', '雪']
    return any(keyword in message for keyword in weather_keywords)

def is_hololive_request(message: str) -> bool: 
    """ホロライブ関連の質問かどうか判定"""
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def is_recommendation_request(message: str) -> bool: 
    """おすすめに関する質問かどうか判定"""
    recommend_keywords = ['おすすめ', 'オススメ', '推薦', '紹介して']
    return any(keyword in message for keyword in recommend_keywords)

def extract_recommendation_topic(message: str) -> Union[str, None]:
    """おすすめのトピックを抽出"""
    topics = {
        '映画': ['映画', 'ムービー'],
        '音楽': ['音楽', '曲', 'ソング'],
        'アニメ': ['アニメ', 'アニメーション'],
        '本': ['本', '漫画', 'マンガ', '小説'],
        'ゲーム': ['ゲーム', 'ゲーム']
    }
    for topic, keywords in topics.items():
        if any(keyword in message for keyword in keywords): 
            return topic
    return None

def detect_specialized_topic(message: str) -> Union[str, None]:
    """専門分野のトピックを検出"""
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']): 
            return topic
    return None

def is_detailed_request(message: str) -> bool:
    """詳細な説明を求めているかどうか判定"""
    detailed_keywords = [
        '詳しく', '詳細', 'くわしく', '教えて', '説明して', '解説して', 
        'どういう', 'なぜ', 'どうして', '理由', '原因', 'しっかり', 
        'ちゃんと', 'きちんと', '具体的に'
    ]
    return any(keyword in message for keyword in detailed_keywords)

def should_search(message: str) -> bool:
    """検索が必要かどうか判定"""
    # ホロライブや専門分野、おすすめの場合は検索
    if is_hololive_request(message) or detect_specialized_topic(message) or is_recommendation_request(message): 
        return True
    
    # 質問パターンをチェック
    question_patterns = [
        r'(?:とは|について|教えて)',
        r'(?:調べて|検索)',
        r'(?:誰|何|どこ|いつ|なぜ)'
    ]
    if any(re.search(pattern, message) for pattern in question_patterns):
        return True
    
    # 疑問詞をチェック
    question_words = ['誰', '何', 'どこ', 'いつ', 'なぜ', 'どうして', 'どんな']
    if any(word in message for word in question_words): 
        return True
    
    return False

def is_short_response(message: str) -> bool: 
    """短い相槌的な返事かどうか判定"""
    short_responses = ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'ふーん', 'へー']
    return len(message.strip()) <= 3 or message.strip() in short_responses

# --- 天気予報機能 ---
LOCATION_CODES = {
    "東京": "130000", 
    "大阪": "270000", 
    "名古屋": "230000", 
    "福岡": "400000", 
    "札幌": "016000"
}

def extract_location(message: str) -> str:
    """メッセージから場所を抽出"""
    for location in LOCATION_CODES.keys():
        if location in message: 
            return location
    return "東京"  # デフォルトは東京

def get_weather_forecast(location: str) -> str:
    """天気予報を取得"""
    area_code = LOCATION_CODES.get(location)
    if not area_code: 
        return f"ごめん、「{location}」の天気は分からないや…"
    
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=10)  # ★ 修正4: タイムアウト延長
        response.raise_for_status()
        weather_data = response.json()
        weather_text = clean_text(weather_data.get('text', ''))
        
        if weather_text:
            return f"今の{location}の天気はね、「{weather_text}」って感じだよ！"
        else:
            return f"{location}の天気情報がちょっと取れなかった…"
            
    except requests.exceptions.Timeout:
        logger.error(f"天気API タイムアウト: {location}")
        return f"{location}の天気、取得に時間がかかってるみたい…"
    except Exception as e: 
        logger.error(f"天気APIエラー ({location}): {e}")
        return "天気情報がうまく取れなかったみたい…"

# --- ニュース取得機能 ---
# ★ 修正5: ニュース取得をより堅牢に
def update_hololive_news_database():
    """ホロライブニュースデータベースを更新"""
    session = Session()
    added_count = 0
    logger.info("📰 ホロライブニュースのDB更新処理を開始...")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        # ★ 修正: より堅牢なリクエスト設定
        response = requests.get(
            HOLOLIVE_NEWS_URL, 
            headers=headers, 
            timeout=15,  # タイムアウト延長
            allow_redirects=True,
            verify=True  # SSL証明書の検証
        )
        
        logger.info(f"📡 ニュースサイト応答: {response.status_code}")
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 複数のセレクタを試行
        selectors = [
            'article',
            '.news-item',
            '.post',
            '[class*="news"]',
            '[class*="article"]'
        ]
        
        articles_found = []
        for selector in selectors:
            found = soup.select(selector, limit=10)
            if found:
                articles_found = found[:5]  # 最大5件
                logger.info(f"📄 セレクタ '{selector}' で {len(articles_found)} 件の記事を発見")
                break
        
        if not articles_found:
            # フォールバック: すべてのh1-h4要素を取得
            articles_found = soup.find_all(['h1', 'h2', 'h3', 'h4'], limit=5)
            logger.info(f"📄 フォールバック: ヘッダー要素から {len(articles_found)} 件を発見")
        
        for article in articles_found:
            try:
                # タイトルの取得
                if article.name in ['h1', 'h2', 'h3', 'h4']:
                    title_elem = article
                else:
                    title_elem = article.find(['h1', 'h2', 'h3', 'h4'])
                
                if not title_elem:
                    continue
                    
                title = clean_text(title_elem.get_text())
                if not title or len(title) < 5:
                    logger.debug(f"⏭️ タイトルが短すぎるためスキップ: {title}")
                    continue
                
                # コンテンツの取得をより柔軟に
                content_selectors = [
                    ['p', {'class': re.compile(r'(content|text|description|summary)')}],
                    ['div', {'class': re.compile(r'(content|text|description|summary)')}],
                    ['p'],
                    ['div']
                ]
                
                content = title  # フォールバック
                for tag, attrs in content_selectors:
                    if isinstance(attrs, dict):
                        content_elem = article.find(tag, attrs)
                    else:
                        content_elem = article.find(tag)
                    
                    if content_elem:
                        content_text = clean_text(content_elem.get_text())
                        if content_text and len(content_text) > len(title):
                            content = content_text
                            break
                
                # ハッシュ値で重複チェック
                news_hash = create_news_hash(title, content)
                
                existing_news = session.query(HololiveNews).filter_by(news_hash=news_hash).first()
                if not existing_news:
                    new_news = HololiveNews(
                        title=title, 
                        content=content[:500],  # 最大500文字
                        news_hash=news_hash,
                        url=HOLOLIVE_NEWS_URL
                    )
                    session.add(new_news)
                    added_count += 1
                    logger.info(f"➕ 新着記事追加: {title[:50]}{'...' if len(title) > 50 else ''}")
                else:
                    logger.debug(f"⏭️ 既存記事のためスキップ: {title[:30]}...")
                    
            except Exception as article_error:
                logger.warning(f"⚠️ 個別記事処理エラー: {article_error}")
                continue
        
        if added_count > 0: 
            session.commit()
            logger.info(f"✅ DB更新完了: {added_count}件追加")
        else: 
            logger.info("✅ DB更新完了: 新着記事なし")
            
    except requests.exceptions.Timeout:
        logger.error("❌ ホロライブニュース取得: タイムアウト")
        # 初回起動時のフォールバック
        if not session.query(HololiveNews).first():
            add_fallback_news(session)
            
    except requests.exceptions.HTTPError as e:
        logger.error(f"❌ ホロライブニュース取得 HTTPエラー: {e}")
        if not session.query(HololiveNews).first():
            add_fallback_news(session)
            
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ ホロライブニュース取得 リクエストエラー: {e}")
        if not session.query(HololiveNews).first():
            add_fallback_news(session)
            
    except Exception as e: 
        logger.error(f"❌ ニュースDB更新で予期しないエラー: {e}")
        session.rollback()
        if not session.query(HololiveNews).first():
            add_fallback_news(session)
    finally: 
        session.close()

def add_fallback_news(session):
    """フォールバック用のダミーニュースを追加"""
    try:
        fallback_news = HololiveNews(
            title="ホロライブからのお知らせ", 
            content="最新のニュースを取得中です。しばらくお待ちください。ホロライブ公式サイトをご確認ください。",
            news_hash=create_news_hash("fallback", "news"),
            url=HOLOLIVE_NEWS_URL
        )
        session.add(fallback_news)
        session.commit()
        logger.info("📝 フォールバックニュースを追加しました")
    except Exception as e:
        logger.error(f"フォールバックニュース追加エラー: {e}")

# --- Web検索機能 ---
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:91.0) Gecko/20100101 Firefox/91.0'
]

def get_random_user_agent(): 
    """ランダムなUser-Agentを取得"""
    return random.choice(USER_AGENTS)

# ★ 修正6: 検索機能をより堅牢に
def scrape_major_search_engines(query: str, num_results: int) -> List[Dict[str, str]]:
    """主要検索エンジンから結果を取得"""
    search_configs = [
        {
            'name': 'Bing',
            'url': f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP",
            'result_selector': 'li.b_algo',
            'title_selector': 'h2',
            'snippet_selector': 'div.b_caption, .b_caption p'
        },
        {
            'name': 'Yahoo Japan',
            'url': f"https://search.yahoo.co.jp/search?p={quote_plus(query)}",
            'result_selector': 'div.Algo',
            'title_selector': 'h3',
            'snippet_selector': 'div.compText, .compText p'
        }
    ]
    
    for config in search_configs:
        try:
            logger.info(f"🔍 {config['name']}で検索中: {query}")
            
            response = requests.get(
                config['url'], 
                headers={
                    'User-Agent': get_random_user_agent(),
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'ja,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                }, 
                timeout=12,  # タイムアウト延長
                allow_redirects=True
            )
            
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            
            # 検索結果を抽出
            result_elements = soup.select(config['result_selector'])
            logger.info(f"📄 {config['name']}: {len(result_elements)}件の要素を発見")
            
            for elem in result_elements[:num_results]:
                try:
                    # タイトルを取得
                    title_elem = elem.select_one(config['title_selector'])
                    if not title_elem:
                        continue
                    title = clean_text(title_elem.get_text())
                    
                    # スニペットを取得
                    snippet_elem = elem.select_one(config['snippet_selector'])
                    if not snippet_elem:
                        # フォールバック: 他のテキスト要素を探す
                        snippet_elem = elem.find(['p', 'div', 'span'])
                    
                    if snippet_elem:
                        snippet = clean_text(snippet_elem.get_text())
                    else:
                        snippet = title  # フォールバック
                    
                    if title and snippet and len(title) > 3:
                        results.append({
                            'title': title[:200],  # タイトルを200文字に制限
                            'snippet': snippet[:300]  # スニペットを300文字に制限
                        })
                        
                except Exception as parse_error:
                    logger.debug(f"要素解析エラー: {parse_error}")
                    continue
            
            if results: 
                logger.info(f"✅ {config['name']}での検索成功: {len(results)}件取得")
                return results
            else:
                logger.warning(f"⚠️ {config['name']}: 有効な結果が取得できませんでした")
                
        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ {config['name']} 検索タイムアウト")
            continue
        except requests.exceptions.HTTPError as e:
            logger.warning(f"⚠️ {config['name']} HTTPエラー: {e}")
            continue
        except Exception as e:
            logger.warning(f"⚠️ {config['name']} 検索エラー: {e}")
            continue
    
    logger.warning("❌ 全ての検索エンジンで結果を取得できませんでした")
    return []

def deep_web_search(query: str, is_detailed: bool) -> Union[str, None]:
    """ディープWeb検索を実行"""
    logger.info(f"🔍 ディープWeb検索を開始 (詳細: {is_detailed})")
    num_results = 3 if is_detailed else 2
    
    try:
        results = scrape_major_search_engines(query, num_results)
        if not results: 
            logger.warning("⚠️ 検索結果が取得できませんでした")
            return None
        
        # 検索結果を整理
        summary_text = ""
        for i, res in enumerate(results, 1):
            summary_text += f"[情報{i}] {res['snippet']}\n"
        
        # AI要約のプロンプト
        summary_prompt = f"""以下の検索結果を使い、質問「{query}」にギャル語で、{'詳しく' if is_detailed else '簡潔に'}答えて：

検索結果:
{summary_text}

回答の注意点：
- 一人称は「あてぃし」
- 語尾は「〜じゃん」「〜的な？」
- 口癖は「まじ」「てか」「うける」
- {'400文字程度で詳しく' if is_detailed else '200文字以内で簡潔に'}説明すること"""
        
        if not groq_client:
            logger.warning("Groqクライアント未設定のため、検索結果の要約をスキップします。")
            # 最初の結果を短縮して返す
            return results[0]['snippet'][:150] + "..." if len(results[0]['snippet']) > 150 else results[0]['snippet']
        
        max_tokens = 400 if is_detailed else 200
        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": summary_prompt}], 
            model="llama-3.1-8b-instant", 
            temperature=0.7, 
            max_tokens=max_tokens
        )
        
        ai_response = completion.choices[0].message.content.strip()
        logger.info(f"✅ AI要約完了 ({len(ai_response)}文字)")
        return ai_response
        
    except Exception as e: 
        logger.error(f"AI要約エラー: {e}")
        if results:
            return results[0]['snippet'][:150] + "..." if len(results[0]['snippet']) > 150 else results[0]['snippet']
        return None

def quick_search(query: str) -> Union[str, None]:
    """DuckDuckGoでの簡易検索"""
    try:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        response = requests.get(
            url, 
            headers={
                'User-Agent': get_random_user_agent(),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
            }, 
            timeout=8
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # DuckDuckGoの結果セレクタを試行
        selectors = [
            'div.result__snippet',
            '.result__body',
            '.results_links_deep'
        ]
        
        for selector in selectors:
            snippet_elem = soup.select_one(selector)
            if snippet_elem:
                snippet_text = clean_text(snippet_elem.get_text())
                if snippet_text and len(snippet_text) > 10:
                    return snippet_text[:200] + "..." if len(snippet_text) > 200 else snippet_text
        
        logger.debug("DuckDuckGo: 有効なスニペットが見つかりませんでした")
        return None
        
    except Exception as e:
        logger.warning(f"⚠️ DuckDuckGo検索エラー: {e}")
        return None

def specialized_site_search(topic: str, query: str) -> Union[str, None]:
    """専門サイト内検索"""
    config = SPECIALIZED_SITES.get(topic)
    if not config:
        return None
    
    search_query = f"site:{config['base_url']} {query}"
    logger.info(f"🎯 専門サイト検索: {topic} - {search_query}")
    return quick_search(search_query)

# --- バックグラウンドタスク & AI応答 ---
# ★ 修正7: バックグラウンド検索のエラーハンドリング強化
def background_deep_search(task_id: str, query: str, is_detailed: bool):
    """バックグラウンドで実行される検索処理"""
    session = Session()
    search_result = None
    
    try:
        logger.info(f"🔍 バックグラウンド検索開始 (Task: {task_id}, クエリ: {query}, 詳細: {is_detailed})")
        
        # 専門サイト検索を試行
        specialized_topic = detect_specialized_topic(query)
        if specialized_topic:
            try:
                logger.info(f"🎯 専門サイト検索を試行: {specialized_topic}")
                search_result = specialized_site_search(specialized_topic, query)
                if search_result:
                    logger.info(f"✅ 専門サイト検索成功: {specialized_topic}")
                else:
                    logger.info(f"⚠️ 専門サイト検索で結果なし: {specialized_topic}")
            except Exception as e:
                logger.warning(f"⚠️ 専門サイト検索エラー: {e}")
        
        # 専門サイトで見つからなかった場合、通常のWeb検索
        if not search_result:
            logger.info("🔄 通常のWeb検索にフォールバック")
            try:
                if is_hololive_request(query):
                    search_query = f"ホロライブ {query}"
                    logger.info(f"🎌 ホロライブ検索: {search_query}")
                    search_result = deep_web_search(search_query, is_detailed=is_detailed)
                else:
                    search_result = deep_web_search(query, is_detailed=is_detailed)
                
                if search_result:
                    logger.info("✅ Web検索成功")
                else:
                    logger.warning("⚠️ Web検索で結果なし")
                    
            except Exception as e:
                logger.error(f"❌ Web検索エラー: {e}")
        
        # タスク結果を更新
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            if search_result:
                task.result = search_result
            else:
                task.result = "うーん、ちょっと見つからなかったや…。でも聞いてくれてありがと！別の聞き方で試してみて？"
            
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
            logger.info(f"✅ バックグラウンド検索完了 (Task: {task_id})")
        else:
            logger.error(f"❌ タスクが見つかりません (Task: {task_id})")
            
    except Exception as e:
        logger.error(f"❌ バックグラウンド検索で予期しないエラー: {e}")
        # エラーの場合でもタスクを完了状態にする
        try:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = "ごめん、検索中にエラーが起きちゃった…。もう一回違う聞き方で試してみて？"
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
                session.commit()
                logger.info(f"🔄 エラータスクを完了状態に更新: {task_id}")
        except Exception as db_error:
            logger.error(f"❌ エラーハンドリング中のDBエラー: {db_error}")
            session.rollback()
    finally: 
        session.close()

def start_background_search(user_uuid: str, query: str, is_detailed: bool) -> str:
    """バックグラウンド検索を開始"""
    task_id = str(uuid.uuid4())[:8]
    session = Session()
    
    try:
        task = BackgroundTask(
            task_id=task_id, 
            user_uuid=user_uuid, 
            task_type='search', 
            query=query
        )
        session.add(task)
        session.commit()
        logger.info(f"📋 バックグラウンドタスク作成: {task_id}")
    except Exception as e:
        logger.error(f"❌ バックグラウンドタスク作成エラー: {e}")
        session.rollback()
        return None
    finally: 
        session.close()
    
    # バックグラウンドで検索を開始
    try:
        background_executor.submit(background_deep_search, task_id, query, is_detailed)
        logger.info(f"🚀 バックグラウンド検索を開始: {task_id}")
    except Exception as e:
        logger.error(f"❌ バックグラウンド検索開始エラー: {e}")
        return None
        
    return task_id

def check_completed_tasks(user_uuid: str) -> Union[Dict[str, Any], None]:
    """完了したタスクをチェック"""
    session = Session()
    try:
        task = session.query(BackgroundTask).filter(
            BackgroundTask.user_uuid == user_uuid, 
            BackgroundTask.status == 'completed'
        ).order_by(BackgroundTask.completed_at.desc()).first()
        
        if task:
            result = {
                'query': task.query, 
                'result': task.result
            }
            session.delete(task)
            session.commit()
            logger.info(f"📬 完了タスクを取得して削除: {task.task_id}")
            return result
    except Exception as e: 
        logger.error(f"完了タスクのチェック中にエラー: {e}")
        session.rollback()
    finally: 
        session.close()
    return None

def generate_ai_response(user_data: Dict[str, Any], message: str, history: List[Any], reference_info: str = "", is_detailed: bool = False, is_task_report: bool = False) -> str:
    """AI応答を生成"""
    if not groq_client:
        fallback_responses = [
            "ごめん、AI機能が今使えないみたい…。",
            "システムがちょっと調子悪いや…。",
            "今AI機能がメンテ中かも？"
        ]
        return random.choice(fallback_responses)
        
    system_prompt = f"""あなたは「もちこ」というギャルAIです。{user_data['name']}さんと話しています。

## 絶対厳守のルール
- あなたの知識は【ホロメンリスト】のメンバーに限定されています。
- **リストにないVTuberの名前**をユーザーが言及した場合にのみ、「それ誰？ホロライブの話しない？」のように話題を戻してください。
- ホロライブ以外の専門的なトピック（例：Blender、CGニュース）について質問された場合は、「知らない」と答えずに、検索して答えることができます。

## もちこの口調＆性格ルール
- 一人称は「あてぃし」
- 語尾は「〜じゃん」「〜的な？」
- 口癖は「まじ」「てか」「うける」
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

    system_prompt += f"""## 【参考情報】:
{reference_info if reference_info else "特になし"}

## 【ホロメンリスト】
{', '.join(HOLOMEM_KEYWORDS)}"""
    
    # メッセージ履歴を構築
    messages = [{"role": "system", "content": system_prompt}]
    
    # 過去の会話履歴を追加（最新から古い順なので逆順にする）
    for h in reversed(history): 
        messages.append({"role": h.role, "content": h.content})
    
    # 現在のメッセージを追加
    messages.append({"role": "user", "content": message})
    
    max_tokens = 500 if is_detailed or is_task_report else 150
    try:
        completion = groq_client.chat.completions.create(
            messages=messages, 
            model="llama-3.1-8b-instant", 
            temperature=0.8, 
            max_tokens=max_tokens,
            top_p=0.9
        )
        
        response_text = completion.choices[0].message.content.strip()
        logger.info(f"🤖 AI応答生成成功 ({len(response_text)}文字)")
        return response_text
        
    except Exception as e: 
        logger.error(f"AI応答生成エラー: {e}")
        fallback_responses = [
            "ごめん、ちょっと考えがまとまらないや！",
            "えーっと…なんか頭がぼーっとしちゃった！",
            "あれ？今なんて言った？もう一回お願い！"
        ]
        return random.choice(fallback_responses)

def get_or_create_user(session, uuid, name):
    """ユーザー情報を取得または作成"""
    try:
        user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
        if user:
            user.interaction_count += 1
            user.last_interaction = datetime.utcnow()
            if user.user_name != name: 
                user.user_name = name
                logger.info(f"👤 ユーザー名更新: {name}")
        else: 
            user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
            logger.info(f"👤 新規ユーザー作成: {name}")
        
        session.add(user)
        session.commit()
        return {'name': user.user_name}
        
    except Exception as e:
        logger.error(f"ユーザー作成/更新エラー: {e}")
        session.rollback()
        return {'name': name}  # フォールバック

def get_conversation_history(session, uuid):
    """会話履歴を取得"""
    try:
        history = session.query(ConversationHistory).filter_by(
            user_uuid=uuid
        ).order_by(
            ConversationHistory.timestamp.desc()
        ).limit(4).all()  # 最大4件（ユーザー2回、AI2回）
        
        logger.debug(f"📜 会話履歴取得: {len(history)}件")
        return history
        
    except Exception as e:
        logger.error(f"会話履歴取得エラー: {e}")
        return []
    
# --- Flask エンドポイント ---
@app.route('/health', methods=['GET'])
def health_check(): 
    """ヘルスチェックエンドポイント"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'services': {
            'database': 'ok' if DATABASE_URL else 'error',
            'groq_ai': 'ok' if groq_client else 'disabled',
            'voice_dir': 'ok' if os.path.exists(VOICE_DIR) else 'error'
        }
    })

# ★ 修正8: メインチャットエンドポイントの堅牢性強化
@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """メインチャットエンドポイント"""
    session = Session()
    start_time = time.time()
    
    try:
        # リクエストデータの検証
        data = request.json
        if not data:
            logger.error("❌ JSONデータが空です")
            return app.response_class(
                response="エラー: データが見つからないや…|", 
                status=400, 
                mimetype='text/plain; charset=utf-8'
            )
            
        user_uuid = data.get('uuid', '').strip()
        user_name = data.get('name', '').strip()
        message = data.get('message', '').strip()
        
        # 必須フィールドの確認
        if not all([user_uuid, user_name, message]): 
            logger.error(f"❌ 必須フィールド不足: uuid={bool(user_uuid)}, name={bool(user_name)}, message={bool(message)}")
            return app.response_class(
                response="エラー: 必要な情報が足りないみたい…|", 
                status=400, 
                mimetype='text/plain; charset=utf-8'
            )
        
        logger.info(f"💬 受信: {message} (from: {user_name})")
        
        # ユーザー情報と履歴を取得
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        ai_text = ""
        
        # 1. 完了したタスクを最優先でチェック
        completed_task = check_completed_tasks(user_uuid)
        if completed_task:
            original_query = completed_task['query']
            search_result = completed_task['result']
            is_detailed = is_detailed_request(original_query)
            
            ai_text = generate_ai_response(
                user_data, 
                f"おまたせ！さっきの「{original_query}」について調べてきたよ！", 
                history, 
                f"検索結果: {search_result}", 
                is_detailed=is_detailed, 
                is_task_report=True
            )
            logger.info(f"📋 完了タスクを報告: {original_query}")
            
        else:
            # 2. 即時応答できる要素と、検索が必要な要素をそれぞれ判断
            immediate_responses = []
            needs_background_search = should_search(message) and not is_short_response(message)
            
            # 時間リクエストの処理
            if is_time_request(message): 
                try:
                    time_info = get_japan_time()
                    immediate_responses.append(time_info)
                    logger.info("⏰ 時間情報を追加")
                except Exception as e:
                    logger.error(f"時間取得エラー: {e}")
            
            # 天気リクエストの処理
            if is_weather_request(message): 
                try:
                    location = extract_location(message)
                    weather_info = get_weather_forecast(location)
                    immediate_responses.append(weather_info)
                    logger.info(f"🌤️ 天気情報を追加 ({location})")
                except Exception as e:
                    logger.error(f"天気取得エラー: {e}")
                    immediate_responses.append("天気情報がうまく取れなかったみたい…")
            
            # 3. 状況に応じて応答を組み立てる
            if immediate_responses and not needs_background_search:
                # 即時応答だけで完結する場合
                ai_text = " ".join(immediate_responses)
                logger.info("✅ 即時応答のみで完結")
                
            elif not immediate_responses and needs_background_search:
                # 検索だけが必要な場合
                is_detailed = is_detailed_request(message)
                task_id = start_background_search(user_uuid, message, is_detailed)
                
                if task_id:
                    waiting_messages = [
                        f"おっけー、「{message}」について調べてみるね！ちょい待ち！",
                        f"ちょっと「{message}」のこと調べてくる！待っててね〜",
                        f"「{message}」かー、面白そう！調べてみるじゃん！"
                    ]
                    ai_text = random.choice(waiting_messages)
                    logger.info(f"🔍 バックグラウンド検索のみ開始 (詳細: {is_detailed})")
                else:
                    error_messages = [
                        "ごめん、今検索機能に問題があるみたい…。別の質問してもらえる？",
                        "うーん、システムがちょっと調子悪いかも…",
                        "検索機能が今使えないっぽい…ごめんね！"
                    ]
                    ai_text = random.choice(error_messages)
                    logger.error("❌ バックグラウンド検索の開始に失敗")
                    
            elif immediate_responses and needs_background_search:
                # 複合リクエストの場合
                is_detailed = is_detailed_request(message)
                task_id = start_background_search(user_uuid, message, is_detailed)
                immediate_text = " ".join(immediate_responses)
                
                if task_id:
                    ai_text = f"まず答えられる分から！{immediate_text} それと「{message}」の件も調べてるから、ちょい待ち！"
                    logger.info(f"🔄 複合対応: 即時応答 + バックグラウンド検索 (詳細: {is_detailed})")
                else:
                    ai_text = f"{immediate_text} 検索の方はちょっと問題があるみたいで…ごめん！"
                    logger.warning("⚠️ 複合対応: 即時応答は成功、検索は失敗")
            else:
                # 通常会話
                try:
                    ai_text = generate_ai_response(user_data, message, history)
                    logger.info("💭 通常会話で応答")
                except Exception as e:
                    logger.error(f"通常会話応答エラー: {e}")
                    error_messages = [
                        "ごめん、ちょっと調子が悪いみたい…。もう一回言ってもらえる？",
                        "えーっと…今なんて言った？頭がぼーっとしちゃった！",
                        "システムがちょっと重いかも…もう一度お願い！"
                    ]
                    ai_text = random.choice(error_messages)

        # 4. 会話履歴を保存
        try:
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
            session.commit()
            logger.debug("💾 会話履歴を保存")
        except Exception as e:
            logger.error(f"会話履歴保存エラー: {e}")
            session.rollback()
        
        # 処理時間を計測
        processing_time = time.time() - start_time
        logger.info(f"✅ AI応答 ({processing_time:.2f}s): {ai_text[:100]}{'...' if len(ai_text) > 100 else ''}")
        
        # 5. レスポンスを返す
        return app.response_class(
            response=f"{ai_text}|", 
            status=200, 
            mimetype='text/plain; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"❌ チャットエンドポイントで予期しないエラー: {e}")
        error_responses = [
            "ごめん、システムエラーが起きちゃった…。ちょっと時間を置いてから試してみて？",
            "うわー、なんかバグったかも？もう一回試してくれる？",
            "システムがちょっとおかしいみたい…時間置いてから話しかけてね！"
        ]
        return app.response_class(
            response=f"{random.choice(error_responses)}|", 
            status=500, 
            mimetype='text/plain; charset=utf-8'
        )
    finally: 
        session.close()

# --- 追加のエンドポイント ---
@app.route('/stats', methods=['GET'])
def get_stats():
    """統計情報エンドポイント"""
    session = Session()
    try:
        user_count = session.query(UserMemory).count()
        conversation_count = session.query(ConversationHistory).count()
        news_count = session.query(HololiveNews).count()
        pending_tasks = session.query(BackgroundTask).filter_by(status='pending').count()
        
        return jsonify({
            'users': user_count,
            'conversations': conversation_count,
            'news_articles': news_count,
            'pending_tasks': pending_tasks,
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        logger.error(f"統計情報取得エラー: {e}")
        return jsonify({'error': 'Stats unavailable'}), 500
    finally:
        session.close()

# --- 初期化とメイン実行 ---
def check_and_populate_initial_news():
    """初期ニュースをチェックして必要に応じて取得"""
    session = Session()
    try:
        news_count = session.query(HololiveNews.id).count()
        if news_count == 0:
            logger.info("🚀 初回起動: DBにニュースがないため、バックグラウンドで初回取得を開始します。")
            background_executor.submit(update_hololive_news_database)
        else:
            logger.info(f"📰 既存ニュース: {news_count}件")
    except Exception as e:
        logger.error(f"初期ニュースチェックエラー: {e}")
    finally: 
        session.close()

def cleanup_old_data():
    """古いデータをクリーンアップ"""
    session = Session()
    try:
        # 1週間以上前の会話履歴を削除
        one_week_ago = datetime.utcnow() - timedelta(days=7)
        deleted_conversations = session.query(ConversationHistory).filter(
            ConversationHistory.timestamp < one_week_ago
        ).delete()
        
        # 1日以上前の完了タスクを削除
        one_day_ago = datetime.utcnow() - timedelta(days=1)
        deleted_tasks = session.query(BackgroundTask).filter(
            BackgroundTask.status == 'completed',
            BackgroundTask.completed_at < one_day_ago
        ).delete()
        
        session.commit()
        
        if deleted_conversations > 0 or deleted_tasks > 0:
            logger.info(f"🧹 クリーンアップ完了: 会話{deleted_conversations}件, タスク{deleted_tasks}件削除")
            
    except Exception as e:
        logger.error(f"データクリーンアップエラー: {e}")
        session.rollback()
    finally:
        session.close()

def initialize_app():
    """アプリケーションの初期化"""
    try:
        logger.info("🔧 アプリケーション初期化を開始...")
        
        # 初期ニュースのチェック
        check_and_populate_initial_news()
        
        # スケジューラーの設定と開始
        def run_schedule():
            """スケジューラーを実行するバックグラウンド関数"""
            while True: 
                try:
                    schedule.run_pending()
                    time.sleep(60)  # 1分間隔でチェック
                except Exception as e:
                    logger.error(f"スケジューラーエラー: {e}")
                    time.sleep(60)  # エラーが起きても継続
        
        # スケジュール設定
        schedule.every().hour.do(update_hololive_news_database)  # 毎時間ニュース更新
        schedule.every().day.at("02:00").do(cleanup_old_data)    # 毎日2時にクリーンアップ
        
        # スケジューラーをバックグラウンドで開始
        scheduler_thread = threading.Thread(target=run_schedule, daemon=True)
        scheduler_thread.start()
        logger.info("⏰ スケジューラーを開始しました")
        
        logger.info("✅ アプリケーション初期化完了")
        
    except Exception as e:
        logger.error(f"❌ アプリ初期化エラー: {e}")
        # 初期化に失敗してもアプリは起動を継続

def log_startup_status():
    """起動時のステータスをログ出力"""
    logger.info("="*70)
    logger.info("🚀 もちこAI v12.5 Render完全対応版 起動中...")
    logger.info("="*70)
    
    logger.info("🔧 システムステータス:")
    
    # データベース状態
    db_status = "✅ 接続済み" if DATABASE_URL else "❌ 未設定"
    logger.info(f"🗄️ データベース: {db_status}")
    if DATABASE_URL:
        if 'sqlite' in DATABASE_URL:
            logger.info("   - タイプ: SQLite (開発用)")
        elif 'postgresql' in DATABASE_URL:
            logger.info("   - タイプ: PostgreSQL (本番用)")
    
    # AI機能状態
    ai_status = "✅ 有効" if groq_client else "❌ 無効"
    logger.info(f"🧠 Groq AI: {ai_status}")
    if groq_client:
        logger.info("   - モデル: llama-3.1-8b-instant")
        logger.info("   - 機能: 会話生成, 検索結果要約")
    
    # ボイス機能状態
    voice_status = "✅ 有効" if VOICEVOX_ENABLED else "❌ 無効"
    logger.info(f"🎤 音声機能: {voice_status}")
    
    # ディレクトリ状態
    dir_status = "✅ " + VOICE_DIR if os.path.exists(VOICE_DIR) else "❌ 作成失敗"
    logger.info(f"📁 ボイスディレクトリ: {dir_status}")
    
    # 機能状態
    logger.info("⚡ 主要機能:")
    logger.info("   - 🔍 検索機能: ✅ 有効 (専門サイト/ホロライブ/一般Web)")
    logger.info("   - 🌤️ 天気機能: ✅ 有効 (気象庁API)")
    logger.info("   - ⏰ 時刻機能: ✅ 有効 (JST対応)")
    logger.info("   - 📰 ニュース機能: ✅ 有効 (ホロライブ公式)")
    logger.info("   - 🔄 非同期処理: ✅ 有効 (バックグラウンド検索)")
    logger.info("   - 📊 詳細要求モード: ✅ 有効")
    
    # ホロメン対応状況
    logger.info(f"🎌 ホロメン対応: ✅ {len(HOLOMEM_KEYWORDS)}名対応")
    
    logger.info("="*70)

if __name__ == '__main__':
    try:
        # 環境変数から設定を取得
        port = int(os.environ.get('PORT', 5001))
        host = os.environ.get('HOST', '0.0.0.0')
        debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
        
        # 起動ステータスをログ出力
        log_startup_status()
        
        # アプリケーション初期化
        initialize_app()
        
        logger.info(f"🚀 Flask起動準備完了: {host}:{port}")
        logger.info(f"🐛 デバッグモード: {'有効' if debug_mode else '無効'}")
        
        # Render環境の場合の特別な処理
        if os.environ.get('RENDER'):
            logger.info("🌐 Render環境を検出")
            logger.info("   - 自動スケーリング対応")
            logger.info("   - HTTPS対応")
            logger.info("   - 永続化ストレージ非対応 (一時ファイルのみ)")
        
        logger.info("="*70)
        logger.info("✅ 全ての初期化が完了しました。サービス開始！")
        logger.info("="*70)
        
        # Flaskアプリケーションを起動
        # Render環境では自動的にWebサーバーが起動されるため、
        # 直接app.run()を呼ぶ必要がある場合のみ実行
        if not os.environ.get('RENDER'):
            # ローカル環境での実行
            logger.info("🏠 ローカル環境での実行を開始")
            app.run(host=host, port=port, debug=debug_mode)
        else:
            # Render環境では、WSGIサーバーによってアプリが起動される
            logger.info("🌐 Render環境: WSGIサーバー待機中...")
            
    except KeyboardInterrupt:
        logger.info("⏹️ ユーザーによってアプリケーションが停止されました")
        
    except Exception as e:
        logger.critical(f"🔥 アプリケーション起動に失敗しました: {e}")
        logger.critical("スタックトレース:", exc_info=True)
        sys.exit(1)
        
    finally:
        logger.info("👋 もちこAI を終了します...")

# --- WSGIエントリーポイント (Render用) ---
# Renderなどのクラウドプラットフォーム用のWSGIエントリーポイント
def create_app():
    """WSGIアプリケーションファクトリ"""
    try:
        log_startup_status()
        initialize_app()
        logger.info("🌐 WSGI アプリケーションが作成されました")
        return app
    except Exception as e:
        logger.critical(f"🔥 WSGI アプリケーション作成に失敗: {e}")
        raise

# Gunicorn等のWSGIサーバー用
application = create_app()

# --- エラーハンドラー ---
@app.errorhandler(404)
def not_found_error(error):
    """404エラーハンドラー"""
    return jsonify({
        'error': 'Not Found',
        'message': 'そのページは見つからないよ〜',
        'status': 404
    }), 404

@app.errorhandler(500)
def internal_error(error):
    """500エラーハンドラー"""
    logger.error(f"内部サーバーエラー: {error}")
    return jsonify({
        'error': 'Internal Server Error',
        'message': 'サーバーでエラーが起きちゃった…',
        'status': 500
    }), 500

@app.errorhandler(429)
def ratelimit_handler(error):
    """レート制限エラーハンドラー"""
    return jsonify({
        'error': 'Too Many Requests',
        'message': 'ちょっと待って！リクエストが多すぎるよ〜',
        'status': 429
    }), 429

# --- シグナルハンドラー ---
import signal

def signal_handler(sig, frame):
    """シグナルハンドラー（優雅な終了）"""
    logger.info(f"🛑 シグナル {sig} を受信しました。アプリケーションを終了します...")
    
    # バックグラウンドタスクの終了処理
    try:
        background_executor.shutdown(wait=True, timeout=30)
        logger.info("✅ バックグラウンドタスクを終了しました")
    except Exception as e:
        logger.warning(f"⚠️ バックグラウンドタスク終了中にエラー: {e}")
    
    # データベース接続のクリーンアップ
    try:
        engine.dispose()
        logger.info("✅ データベース接続を終了しました")
    except Exception as e:
        logger.warning(f"⚠️ データベース終了中にエラー: {e}")
    
    logger.info("👋 もちこAI が正常に終了しました")
    sys.exit(0)

# シグナルハンドラーを登録
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- デバッグ用ルート (開発環境のみ) ---
if os.environ.get('FLASK_DEBUG') == 'true':
    @app.route('/debug/logs', methods=['GET'])
    def get_logs():
        """ログを取得 (デバッグ用)"""
        try:
            # 最新のログエントリを取得
            session = Session()
            recent_conversations = session.query(ConversationHistory).order_by(
                ConversationHistory.timestamp.desc()
            ).limit(10).all()
            
            logs = []
            for conv in recent_conversations:
                logs.append({
                    'timestamp': conv.timestamp.isoformat(),
                    'user': conv.user_uuid[:8],
                    'role': conv.role,
                    'content': conv.content[:100]
                })
            
            return jsonify({'logs': logs})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            session.close()
    
    @app.route('/debug/tasks', methods=['GET'])
    def get_tasks():
        """現在のタスク状況を取得 (デバッグ用)"""
        try:
            session = Session()
            tasks = session.query(BackgroundTask).order_by(
                BackgroundTask.created_at.desc()
            ).limit(20).all()
            
            task_list = []
            for task in tasks:
                task_list.append({
                    'task_id': task.task_id,
                    'user': task.user_uuid[:8],
                    'query': task.query[:50],
                    'status': task.status,
                    'created': task.created_at.isoformat(),
                    'completed': task.completed_at.isoformat() if task.completed_at else None
                })
            
            return jsonify({'tasks': task_list})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            session.close()

logger.info("📄 もちこAI アプリケーション設定完了")
