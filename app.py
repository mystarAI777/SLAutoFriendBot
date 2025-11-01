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
import unicodedata
from groq import Groq
import google.generativeai as genai
from flask import Response

# 型ヒント用のインポート（エラー対策付き）
try:
    from typing import Union, Dict, Any, List, Optional
except ImportError:
    # 型ヒントが使えない環境用のフォールバック
    Dict = dict
    Any = object
    List = list
    Union = object
    Optional = object

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
import signal

# --- 基本設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 定数設定 ---
VOICE_DIR = '/tmp/voices'
SERVER_URL = "https://slautofriendbot.onrender.com"
VOICEVOX_SPEAKER_ID = 20  # もち子さん(ノーマル) に統合
HOLOLIVE_NEWS_URL = "https://hololive-tsuushin.com/category/holonews/"

SL_SAFE_CHAR_LIMIT = 300      # Second Life安全文字数制限
VOICE_OPTIMAL_LENGTH = 150    # VOICEVOX最適文字数

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
]
LOCATION_CODES = {
    "東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"
}
SPECIALIZED_SITES = {
    'Blender': {
        'base_url': 'https://docs.blender.org/manual/ja/latest/',
        'keywords': ['Blender', 'ブレンダー', 'blender', 'BLENDER']
    },
    'CGニュース': {
        'base_url': 'https://modelinghappy.com/',
        'keywords': ['CGニュース', '3DCG', 'CG', 'ＣＧ', 'ｃｇ', 'cg', '3dcg', '３ＤＣＧ', 'CG業界', 'CGアニメ']
    },
    '脳科学・心理学': {
        'base_url': 'https://nazology.kusuguru.co.jp/',
        'keywords': ['脳科学', '心理学', '脳', '心理', 'のうかがく', 'しんりがく']
    },
    'セカンドライフ': {
        'base_url': 'https://community.secondlife.com/news/',
        'keywords': ['セカンドライフ', 'Second Life', 'SL', 'second life', 'セカンド', 'SecondLife']
    },
    'アニメ': {
    'base_url': 'https://animedb.jp/',
    'keywords': ['アニメ', 'anime', 'ANIME', 'ｱﾆﾒ', 'アニメーション', '作画', '声優', 'OP', 'ED']
}
}
ANIME_KEYWORDS = [
    'アニメ', 'anime', 'ANIME', 'ｱﾆﾒ', 'アニメーション',
    '作画', '声優', 'OP', 'ED', 'オープニング', 'エンディング',
    '劇場版', '映画', 'OVA', 'OAD', '原作', '漫画', 'ラノベ',
    '主人公', 'キャラ', 'キャラクター', '制作会社', 'スタジオ'
]
# --- グローバル変数 & Executor ---
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client = None
VOICEVOX_ENABLED = True
app = Flask(__name__)
CORS(app)

# ↓↓↓ ここに追加 ↓↓↓
@app.after_request
def after_request(response):
    """CORSヘッダーを全レスポンスに追加"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

Base = declarative_base()

# ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼【ここからが唯一の変更箇所です】▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼

# --- 秘密情報/環境変数 読み込み ---
def get_secret(name):
    """
    まずRenderのSecret Fileから秘密情報を読み込み、見つからなければ環境変数から読み込む。
    """
    secret_file_path = f"/etc/secrets/{name}"
    if os.path.exists(secret_file_path):
        try:
            with open(secret_file_path, 'r') as f:
                logger.info(f"✅ Secret Fileから {name} を読み込みました。")
                return f.read().strip()
        except IOError as e:
            logger.error(f"❌ Secret File {secret_file_path} の読み込みに失敗: {e}")
            return None
    
    # Secret Fileが見つからない場合は、フォールバックとして環境変数をチェック
    value = os.environ.get(name)
    if value:
         logger.info(f"✅ 環境変数から {name} を読み込みました。")
    return value

def ensure_voice_directory():
    """音声ディレクトリの存在を保証"""
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            if not os.path.exists(VOICE_DIR):
                os.makedirs(VOICE_DIR, mode=0o755, exist_ok=True)
                logger.info(f"✅ Voice directory created: {VOICE_DIR}")
            
            # 書き込み権限を確認
            if os.access(VOICE_DIR, os.W_OK):
                logger.info(f"✅ Voice directory is writable: {VOICE_DIR}")
                return True
            else:
                # 権限を修正
                os.chmod(VOICE_DIR, 0o755)
                logger.info(f"✅ Voice directory permissions fixed: {VOICE_DIR}")
                return True
                
        except Exception as e:
            logger.error(f"❌ Voice directory creation failed (attempt {attempt + 1}/{max_attempts}): {e}")
            if attempt < max_attempts - 1:
                time.sleep(1)
            continue
    
    logger.critical(f"🔥 Failed to create voice directory after {max_attempts} attempts")
    return False
    
DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')
gemini_model = None

def initialize_gemini_client():
    global gemini_model
    try:
        if GEMINI_API_KEY and len(GEMINI_API_KEY) > 20:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
            logger.info("✅ Gemini 2.0 Flash client initialized")
    except Exception as e:
        logger.error(f"❌ Gemini initialization failed: {e}")

# --- Hololive Wiki検索機能の追加 ---
def search_hololive_wiki(member_name, query_topic):
    """
    SeesaawikiのホロライブWikiから情報を検索する。
    メンバー名と特定のトピックを組み合わせて検索クエリを生成。
    """
    base_url = "https://seesaawiki.jp/hololivetv/"
    search_query = f"{member_name} {query_topic}"
    encoded_query = quote_plus(search_query.encode('euc-jp')) # SeesaawikiはEUC-JPが多い
    search_url = f"{base_url}search?query={encoded_query}"
    
    try:
        logger.info(f"🔍 Searching Hololive Wiki for: {search_query} at {search_url}")
        response = requests.get(
            search_url,
            headers={'User-Agent': random.choice(USER_AGENTS)},
            timeout=15,
            allow_redirects=True
        )
        # Seesaawikiのエンコーディングに合わせてデコードを試みる
        response.encoding = 'euc-jp'
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 検索結果ページから関連性の高いコンテンツを探す
        # ページ全体のテキストを取得し、関連部分を抽出するアプローチ
        
        # まず、メインコンテンツエリアを特定
        main_content_div = soup.find('div', id='pagebody') or soup.find('div', class_='contents')
        if not main_content_div:
            logger.warning("Hololive Wiki: Could not find main content div.")
            return None

        # 関連性の高い情報を抽出するためのキーワード検索
        page_text = clean_text(main_content_div.get_text())
        
        # メンバー名とトピックを含む周辺の文章を抽出する
        # 例: 「さくらみこ」と「マイクラ」で検索した場合、「さくらみこはマイクラで独特の建築をする」のような文章
        
        # 簡易的な要約生成
        # 検索キーワードが含まれる文をいくつか抽出
        sentences = re.split(r'(。|．|\n)', page_text)
        relevant_sentences = []
        for i in range(0, len(sentences), 2):
            sentence = sentences[i]
            if member_name in sentence and query_topic in sentence:
                relevant_sentences.append(sentence.strip())
            if len(" ".join(relevant_sentences)) > 500: # ある程度の長さに達したら終了
                break

        if relevant_sentences:
            extracted_info = " ".join(relevant_sentences)[:1000] # 最大1000文字
            logger.info(f"✅ Hololive Wiki search successful for '{search_query}'. Extracted: {extracted_info[:100]}")
            return extracted_info
        
        logger.info(f"ℹ️ Hololive Wiki search for '{search_query}' found no direct relevant sentences. Attempting general summary.")
        # 関連文章が見つからなければ、ページの最初の部分を要約として返す
        return page_text[:500] if page_text else None
        
    except requests.exceptions.Timeout:
        logger.warning(f"⚠️ Hololive Wiki search timeout for {search_query}")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"⚠️ Hololive Wiki search request error for {search_query}: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Hololive Wiki search general error for {search_query}: {e}", exc_info=True)
        return None

# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲【変更箇所はここまでです】▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲
# ===== 【追加】アニメ検索機能 =====

def is_anime_request(message):
    """アニメ関連の質問かどうか判定"""
    message_normalized = unicodedata.normalize('NFKC', message).lower()
    
    # アニメキーワードが含まれているか
    for keyword in ANIME_KEYWORDS:
        keyword_normalized = unicodedata.normalize('NFKC', keyword).lower()
        if keyword_normalized in message_normalized:
            return True
    
    # 「〜ってアニメ」「〜というアニメ」などのパターン
    anime_patterns = [
        r'ってアニメ', r'というアニメ', r'のアニメ',
        r'アニメで', r'アニメの', r'アニメは'
    ]
    for pattern in anime_patterns:
        if re.search(pattern, message):
            return True
    
    return False


def search_anime_database(query, is_detailed=False):
    """
    https://animedb.jp/ からアニメ情報を検索
    """
    base_url = "https://animedb.jp/"
    
    try:
        logger.info(f"🎬 Searching anime database for: {query}")
        
        # Step 1: 検索ページにアクセス
        search_url = f"{base_url}search?q={quote_plus(query)}"
        response = requests.get(
            search_url,
            headers={'User-Agent': random.choice(USER_AGENTS)},
            timeout=15,
            allow_redirects=True
        )
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Step 2: 検索結果を解析
        # animedb.jpの構造に合わせて調整（実際のHTML構造を確認して修正してください）
        results = []
        
        # 検索結果のセレクタパターン（複数試行）
        result_selectors = [
            'div.anime-item',
            'div.search-result',
            'article.anime',
            'div[class*="anime"]',
            'li.anime-list-item'
        ]
        
        result_elements = []
        for selector in result_selectors:
            result_elements = soup.select(selector)
            if result_elements:
                logger.info(f"✅ Found results with selector: {selector}")
                break
        
        if not result_elements:
            # セレクタで見つからない場合、全体から情報抽出を試みる
            logger.warning("⚠️ No specific selectors found, trying general extraction")
            # タイトルとあらすじを含むdivを探す
            potential_results = soup.find_all(['div', 'article'], limit=10)
            result_elements = [elem for elem in potential_results if elem.find(['h2', 'h3', 'h4'])]
        
        for elem in result_elements[:3 if is_detailed else 2]:
            # タイトル抽出
            title_elem = elem.find(['h2', 'h3', 'h4', 'a'])
            if not title_elem:
                continue
            
            title = clean_text(title_elem.get_text())
            
            # あらすじ/説明抽出
            description_elem = elem.find(['p', 'div'], class_=lambda x: x and ('description' in x.lower() or 'summary' in x.lower()))
            if not description_elem:
                description_elem = elem.find('p')
            
            description = clean_text(description_elem.get_text()) if description_elem else ""
            
            # リンク抽出
            link_elem = elem.find('a', href=True)
            link = urljoin(base_url, link_elem['href']) if link_elem else ""
            
            if title and len(title) > 2:
                results.append({
                    'title': title,
                    'description': description[:300] if description else "詳細情報なし",
                    'url': link
                })
        
        if not results:
            logger.warning(f"⚠️ No anime results found for: {query}")
            return None
        
        # Step 3: 結果を整形
        formatted_results = []
        for i, result in enumerate(results, 1):
            formatted_results.append(
                f"【{i}】{result['title']}\n"
                f"{result['description'][:150]}..."
            )
        
        summary = "\n\n".join(formatted_results)
        logger.info(f"✅ Anime search successful: {len(results)} results")
        
        return summary
        
    except requests.exceptions.Timeout:
        logger.error(f"❌ Anime search timeout for: {query}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Anime search request error: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Anime search general error: {e}", exc_info=True)
        return None


# ===== 【追加】心理分析機能 =====

def analyze_user_psychology(user_uuid):
    """
    ユーザーの過去の会話履歴から心理分析を実行
    """
    session = Session()
    try:
        logger.info(f"🧠 Starting psychology analysis for user: {user_uuid}")
        
        # Step 1: 会話履歴を取得（最新100件）
        conversations = session.query(ConversationHistory).filter_by(
            user_uuid=user_uuid,
            role='user'  # ユーザーの発言のみ
        ).order_by(ConversationHistory.timestamp.desc()).limit(100).all()
        
        if len(conversations) < 10:
            logger.warning(f"⚠️ Not enough conversation data for analysis: {len(conversations)} messages")
            return None
        
        # Step 2: 会話データを整形
        messages_text = "\n".join([conv.content for conv in reversed(conversations)])
        total_messages = len(conversations)
        avg_length = sum(len(conv.content) for conv in conversations) // total_messages
        
        # Step 3: ユーザー情報を取得
        user_memory = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        user_name = user_memory.user_name if user_memory else "不明"
        
        # Step 4: AI による心理分析
        if not groq_client:
            logger.warning("⚠️ Groq client unavailable, skipping AI analysis")
            return None
        
        analysis_prompt = f"""あなたは心理学の専門家です。以下のユーザー「{user_name}」さんの過去の会話（{total_messages}件）を分析し、心理プロファイルを作成してください。

【会話履歴】
{messages_text[:3000]}

【分析項目】
1. **ビッグファイブ性格特性**（各0-100点で評価）
   - 開放性（Openness）: 新しい経験への興味、創造性
   - 誠実性（Conscientiousness）: 計画性、責任感
   - 外向性（Extraversion）: 社交性、活発さ
   - 協調性（Agreeableness）: 優しさ、協力的
   - 神経症傾向（Neuroticism）: 不安、感情の安定性

2. **興味・関心**（主要な興味分野とその強度）

3. **コミュニケーションスタイル**（カジュアル/丁寧/熱心など）

4. **感情傾向**（ポジティブ/ニュートラル/感情豊かなど）

5. **よく話す話題トップ3**

6. **総合的な人物像の要約**（200文字程度）

**重要**: 以下のJSON形式で回答してください（他の文章は不要）:
{{
  "openness": 75,
  "conscientiousness": 60,
  "extraversion": 80,
  "agreeableness": 70,
  "neuroticism": 40,
  "interests": {{"アニメ": 90, "ゲーム": 70, "音楽": 60}},
  "conversation_style": "カジュアルで親しみやすい",
  "emotional_tendency": "ポジティブで明るい",
  "favorite_topics": ["アニメ", "日常の出来事", "趣味"],
  "summary": "明るく社交的な性格で、アニメや創作活動に強い興味を持つ。カジュアルな会話を好み、感情表現が豊か。新しいことへの好奇心が旺盛。",
  "confidence": 85
}}"""

        try:
            completion = groq_client.chat.completions.create(
                messages=[{"role": "user", "content": analysis_prompt}],
                model="llama-3.1-8b-instant",
                temperature=0.3,  # 分析は正確性重視
                max_tokens=800
            )
            
            response_text = completion.choices[0].message.content.strip()
            
            # JSONを抽出（```json ... ``` の場合に対応）
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
            if json_match:
                response_text = json_match.group(1)
            
            # JSON パース
            analysis_data = json.loads(response_text)
            
            logger.info(f"✅ AI analysis completed for user: {user_uuid}")
            
            # Step 5: データベースに保存
            psychology = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            
            if psychology:
                # 既存データを更新
                psychology.user_name = user_name
                psychology.openness = analysis_data.get('openness', 50)
                psychology.conscientiousness = analysis_data.get('conscientiousness', 50)
                psychology.extraversion = analysis_data.get('extraversion', 50)
                psychology.agreeableness = analysis_data.get('agreeableness', 50)
                psychology.neuroticism = analysis_data.get('neuroticism', 50)
                psychology.interests = json.dumps(analysis_data.get('interests', {}), ensure_ascii=False)
                psychology.favorite_topics = json.dumps(analysis_data.get('favorite_topics', []), ensure_ascii=False)
                psychology.conversation_style = analysis_data.get('conversation_style', '')
                psychology.emotional_tendency = analysis_data.get('emotional_tendency', '')
                psychology.analysis_summary = analysis_data.get('summary', '')
                psychology.total_messages = total_messages
                psychology.avg_message_length = avg_length
                psychology.last_analyzed = datetime.utcnow()
                psychology.analysis_confidence = analysis_data.get('confidence', 70)
            else:
                # 新規作成
                psychology = UserPsychology(
                    user_uuid=user_uuid,
                    user_name=user_name,
                    openness=analysis_data.get('openness', 50),
                    conscientiousness=analysis_data.get('conscientiousness', 50),
                    extraversion=analysis_data.get('extraversion', 50),
                    agreeableness=analysis_data.get('agreeableness', 50),
                    neuroticism=analysis_data.get('neuroticism', 50),
                    interests=json.dumps(analysis_data.get('interests', {}), ensure_ascii=False),
                    favorite_topics=json.dumps(analysis_data.get('favorite_topics', []), ensure_ascii=False),
                    conversation_style=analysis_data.get('conversation_style', ''),
                    emotional_tendency=analysis_data.get('emotional_tendency', ''),
                    analysis_summary=analysis_data.get('summary', ''),
                    total_messages=total_messages,
                    avg_message_length=avg_length,
                    analysis_confidence=analysis_data.get('confidence', 70)
                )
                session.add(psychology)
            
            session.commit()
            logger.info(f"💾 Psychology analysis saved for user: {user_uuid}")
            
            return psychology
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ Failed to parse AI analysis JSON: {e}")
            logger.error(f"Raw response: {response_text[:500]}")
            return None
        except Exception as e:
            logger.error(f"❌ AI analysis error: {e}", exc_info=True)
            return None
            
    except Exception as e:
        logger.error(f"❌ Psychology analysis error: {e}", exc_info=True)
        session.rollback()
        return None
    finally:
        session.close()


def get_user_psychology(user_uuid):
    """ユーザーの心理分析結果を取得"""
    session = Session()
    try:
        psychology = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        
        if not psychology:
            return None
        
        return {
            'openness': psychology.openness,
            'conscientiousness': psychology.conscientiousness,
            'extraversion': psychology.extraversion,
            'agreeableness': psychology.agreeableness,
            'neuroticism': psychology.neuroticism,
            'interests': json.loads(psychology.interests) if psychology.interests else {},
            'favorite_topics': json.loads(psychology.favorite_topics) if psychology.favorite_topics else [],
            'conversation_style': psychology.conversation_style,
            'emotional_tendency': psychology.emotional_tendency,
            'summary': psychology.analysis_summary,
            'confidence': psychology.analysis_confidence,
            'last_analyzed': psychology.last_analyzed
        }
    finally:
        session.close()
        
# --- 初期化処理 ---
ensure_voice_directory()

if not DATABASE_URL:
    logger.critical("FATAL: DATABASE_URL is not set.")
    sys.exit(1)

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

class SpecializedNews(Base):
    __tablename__ = 'specialized_news'
    id = Column(Integer, primary_key=True)
    site_name = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000))
    published_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    news_hash = Column(String(100), unique=True)

# ========================================
# 【変更1】HolomemWiki テーブル定義の拡張
# ========================================

class HolomemWiki(Base):
    __tablename__ = 'holomem_wiki'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text)
    debut_date = Column(String(100))
    generation = Column(String(100))
    tags = Column(Text)
    # 卒業情報
    graduation_date = Column(String(100), nullable=True)
    graduation_reason = Column(Text, nullable=True)
    mochiko_feeling = Column(Text, nullable=True)
    # ★ 追加: 現役/卒業フラグ
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    # ★ 追加: プロフィールURL（情報取得元）
    profile_url = Column(String(500), nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow)


# ========================================
# 【変更2】ホロメン情報を自動取得する関数
# ========================================

def scrape_hololive_members():
    """
    ホロライブ公式サイトからメンバー情報を取得してDBに保存
    公式サイト: https://hololive.hololivepro.com/talents/
    """
    base_url = "https://hololive.hololivepro.com"
    talents_url = f"{base_url}/talents/"
    
    session = Session()
    added_count = 0
    updated_count = 0
    
    try:
        logger.info("🔍 Scraping Hololive members from official site...")
        
        response = requests.get(
            talents_url,
            headers={'User-Agent': random.choice(USER_AGENTS)},
            timeout=20,
            allow_redirects=True
        )
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # メンバーカードを探す（実際のHTML構造に合わせて調整）
        member_cards = soup.select('.talent-card, .member-card, [class*="talent"], [class*="member"]')
        
        if not member_cards:
            # フォールバック: リンクからメンバー名を抽出
            logger.warning("⚠️ Member cards not found, trying fallback method...")
            member_links = soup.find_all('a', href=lambda x: x and '/talents/' in x)
            member_cards = member_links
        
        logger.info(f"📋 Found {len(member_cards)} potential member entries")
        
        for card in member_cards:
            try:
                # メンバー名を取得
                name_elem = card.find(['h2', 'h3', 'h4', 'span', 'div'], class_=lambda x: x and ('name' in x.lower() or 'title' in x.lower()))
                
                if not name_elem:
                    # テキストから直接取得を試みる
                    name_elem = card
                
                member_name = clean_text(name_elem.get_text())
                
                # 不要な文字を除去
                member_name = re.sub(r'\s*\(.*?\)\s*', '', member_name)  # (EN)などを除去
                member_name = member_name.strip()
                
                if not member_name or len(member_name) < 2:
                    continue
                
                # プロフィールURLを取得
                profile_link = card.get('href') or (card.find('a', href=True) or {}).get('href', '')
                if profile_link and not profile_link.startswith('http'):
                    profile_link = urljoin(base_url, profile_link)
                
                # 期（ジェネレーション）を推測
                generation = "不明"
                card_text = card.get_text()
                
                gen_patterns = [
                    (r'0期生|ゼロ期生', '0期生'),
                    (r'1期生|一期生', '1期生'),
                    (r'2期生|二期生', '2期生'),
                    (r'3期生|三期生', '3期生'),
                    (r'4期生|四期生', '4期生'),
                    (r'5期生|五期生', '5期生'),
                    (r'6期生|六期生', '6期生'),
                    (r'ゲーマーズ|GAMERS', 'ゲーマーズ'),
                    (r'ID|Indonesia', 'ホロライブID'),
                    (r'EN|English|Myth|Council|Promise|Advent', 'ホロライブEN'),
                    (r'DEV_IS|ReGLOSS', 'DEV_IS'),
                ]
                
                for pattern, gen_name in gen_patterns:
                    if re.search(pattern, card_text, re.IGNORECASE):
                        generation = gen_name
                        break
                
                # 既存メンバーをチェック
                existing = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                
                if existing:
                    # 更新（現役メンバーとしてマーク）
                    existing.is_active = True
                    existing.generation = generation
                    existing.profile_url = profile_link
                    existing.last_updated = datetime.utcnow()
                    updated_count += 1
                    logger.info(f"🔄 Updated member: {member_name}")
                else:
                    # 新規追加
                    new_member = HolomemWiki(
                        member_name=member_name,
                        description=f"{member_name}は{generation}のメンバーです。",
                        generation=generation,
                        is_active=True,
                        profile_url=profile_link,
                        tags=json.dumps([generation], ensure_ascii=False)
                    )
                    session.add(new_member)
                    added_count += 1
                    logger.info(f"➕ Added new member: {member_name}")
                
            except Exception as e:
                logger.warning(f"⚠️ Error processing member card: {e}")
                continue
        
        # ★ 卒業メンバーの検出
        # 公式サイトに存在しないメンバーを「卒業済み」としてマーク
        all_db_members = session.query(HolomemWiki).filter_by(is_active=True).all()
        
        for db_member in all_db_members:
            # 今回のスクレイピングで更新されなかった = 公式サイトに存在しない
            if db_member.last_updated < datetime.utcnow() - timedelta(minutes=5):
                # 卒業メンバーとしてマーク（ただし、すでに卒業情報がある場合は除く）
                if not db_member.graduation_date:
                    logger.warning(f"⚠️ Member not found on official site: {db_member.member_name} (marking as potentially graduated)")
                    # is_activeをFalseにはしない（手動で卒業情報を追加するまで）
        
        session.commit()
        logger.info(f"✅ Hololive members sync complete: Added {added_count}, Updated {updated_count}")
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Failed to scrape Hololive members: {e}")
        session.rollback()
    except Exception as e:
        logger.error(f"❌ Hololive members scraping error: {e}", exc_info=True)
        session.rollback()
    finally:
        session.close()


# ========================================
# 【変更3】卒業メンバー情報を取得する関数
# ========================================

def scrape_graduated_members():
    """
    ホロライブ卒業メンバーの情報をWikipediaや公式発表から取得
    """
    session = Session()
    
    # 既知の卒業メンバー情報（最低限のデータ）
    known_graduated = [
        {
            'member_name': '夜空メル',
            'generation': '1期生',
            'graduation_date': '2024年1月16日',
            'graduation_reason': '機密情報の漏洩など契約違反行為が認められたため、契約解除となりました。',
            'mochiko_feeling': 'メル先輩、初期からのホロライブを支えてくれてありがと。突然で…言葉が出ないよ…',
            'description': 'ホロライブ1期生。ヴァンパイアの女の子で、アセロラジュースが大好き。',
            'debut_date': '2018年5月13日',
            'tags': ['ヴァンパイア', '癒し声', '1期生', '卒業生']
        },
        {
            'member_name': '潤羽るしあ',
            'generation': '3期生',
            'graduation_date': '2022年2月24日',
            'graduation_reason': '情報漏洩などの契約違反行為や信用失墜行為が認められたため、契約解除となりました。',
            'mochiko_feeling': 'るしあちゃんのこと、今でも信じられないよ…また3期生のみんなでわちゃわちゃしてほしかったな…',
            'description': 'ホロライブ3期生。魔界学校に通うネクロマンサーの女の子。',
            'debut_date': '2019年7月18日',
            'tags': ['ネクロマンサー', '感情豊か', '3期生', '卒業生']
        },
        {
            'member_name': '桐生ココ',
            'generation': '4期生',
            'graduation_date': '2021年7月1日',
            'graduation_reason': '本人の意向を尊重する形で卒業。',
            'mochiko_feeling': '会長がいないの、まじ寂しいじゃん…でも、会長の伝説はホロライブで永遠に語り継がれるよね！',
            'description': '人間の文化に興味を持つドラゴン。日本語と英語を駆使した配信で海外ファンを爆発的に増やした立役者。',
            'debut_date': '2019年12月28日',
            'tags': ['ドラゴン', 'バイリンガル', '伝説', '会長', '卒業生']
        },
        {
            'member_name': '魔乃アロエ',
            'generation': '5期生',
            'graduation_date': '2020年8月31日',
            'graduation_reason': 'デビュー直後の情報漏洩トラブルにより卒業。',
            'mochiko_feeling': 'アロエちゃん、一瞬だったけどキラキラしてた…もっと一緒に活動したかったな、まじで…',
            'description': '魔界でウワサの生意気なサキュバスの子供。',
            'debut_date': '2020年8月15日',
            'tags': ['サキュバス', '5期生', '幻', '卒業生']
        },
        {
            'member_name': '九十九佐命',
            'generation': 'ホロライブEN',
            'graduation_date': '2022年7月31日',
            'graduation_reason': '長期的な活動が困難になったため。',
            'mochiko_feeling': 'サナちゃん、宇宙みたいに心が広くて大好きだったよ。ゆっくり休んで、元気でいてほしいな…',
            'description': 'ホロライブEnglish -Council-所属。「空間」の概念の代弁者。',
            'debut_date': '2021年8月23日',
            'tags': ['宇宙', '癒し', 'EN', '卒業生']
        },
    ]
    
    try:
        for grad_data in known_graduated:
            existing = session.query(HolomemWiki).filter_by(member_name=grad_data['member_name']).first()
            
            if existing:
                # 既存データを更新
                existing.is_active = False
                existing.graduation_date = grad_data['graduation_date']
                existing.graduation_reason = grad_data['graduation_reason']
                existing.mochiko_feeling = grad_data['mochiko_feeling']
                existing.last_updated = datetime.utcnow()
                logger.info(f"🔄 Updated graduated member: {grad_data['member_name']}")
            else:
                # 新規追加
                new_grad = HolomemWiki(
                    member_name=grad_data['member_name'],
                    description=grad_data['description'],
                    debut_date=grad_data['debut_date'],
                    generation=grad_data['generation'],
                    is_active=False,
                    graduation_date=grad_data['graduation_date'],
                    graduation_reason=grad_data['graduation_reason'],
                    mochiko_feeling=grad_data['mochiko_feeling'],
                    tags=json.dumps(grad_data['tags'], ensure_ascii=False)
                )
                session.add(new_grad)
                logger.info(f"➕ Added graduated member: {grad_data['member_name']}")
        
        session.commit()
        logger.info("✅ Graduated members sync complete")
        
    except Exception as e:
        logger.error(f"❌ Graduated members sync error: {e}", exc_info=True)
        session.rollback()
    finally:
        session.close()


# ========================================
# 【変更4】ホロライブニュース更新時にメンバー情報も更新
# ========================================

def update_hololive_news_database():
    """ホロライブニュース + メンバー情報を同時更新"""
    session = Session()
    
    # 1. ニュース更新（既存処理）
    _update_news_database(
        session, 
        HololiveNews, 
        "Hololive", 
        HOLOLIVE_NEWS_URL, 
        ['article', '.post', '.entry', '[class*="post"]', '[class*="article"]']
    )
    
    session.close()
    
    # 2. メンバー情報更新（新規処理）
    logger.info("🔄 Updating Hololive members information...")
    
    # 現役メンバー情報を取得
    scrape_hololive_members()
    
    # 卒業メンバー情報を取得
    scrape_graduated_members()
    
    logger.info("✅ Hololive news + members update complete")


class FriendRegistration(Base):
    __tablename__ = 'friend_registrations'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    friend_uuid = Column(String(255), nullable=False)
    friend_name = Column(String(255), nullable=False)
    registered_at = Column(DateTime, default=datetime.utcnow)
    relationship_note = Column(Text)

class NewsCache(Base):
    __tablename__ = 'news_cache'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    news_id = Column(Integer, nullable=False)
    news_number = Column(Integer, nullable=False)
    news_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserPsychology(Base):
    """ユーザーの心理分析結果を保存"""
    __tablename__ = 'user_psychology'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    
    # 性格分析（ビッグファイブ）
    openness = Column(Integer, default=50)           # 開放性 (0-100)
    conscientiousness = Column(Integer, default=50)  # 誠実性 (0-100)
    extraversion = Column(Integer, default=50)       # 外向性 (0-100)
    agreeableness = Column(Integer, default=50)      # 協調性 (0-100)
    neuroticism = Column(Integer, default=50)        # 神経症傾向 (0-100)
    
    # 興味・関心
    interests = Column(Text)  # JSON形式: {"アニメ": 80, "ゲーム": 60, ...}
    favorite_topics = Column(Text)  # よく話す話題
    
    # コミュニケーションスタイル
    conversation_style = Column(String(50))  # "カジュアル", "丁寧", "熱心" など
    emotional_tendency = Column(String(50))  # "ポジティブ", "ニュートラル", "感情豊か" など
    
    # 統計情報
    total_messages = Column(Integer, default=0)
    avg_message_length = Column(Integer, default=0)
    
    # メタ情報
    analysis_summary = Column(Text)  # AI生成の要約
    last_analyzed = Column(DateTime, default=datetime.utcnow)
    analysis_confidence = Column(Integer, default=0)  # 信頼度 (0-100)

# ===== 改善版: データベースエンジン作成 =====
def create_optimized_db_engine():
    """環境に応じて最適化されたDBエンジンを作成"""
    try:
        is_sqlite = 'sqlite' in DATABASE_URL.lower()
        
        if is_sqlite:
            connect_args = {
                'check_same_thread': False,
                'timeout': 20
            }
            engine = create_engine(
                DATABASE_URL,
                connect_args=connect_args,
                pool_pre_ping=True,
                echo=False
            )
        else:
            # PostgreSQL用の設定
            connect_args = {
                'connect_timeout': 10,
                'options': '-c statement_timeout=30000'
            }
            engine = create_engine(
                DATABASE_URL,
                connect_args=connect_args,
                pool_size=10,
                max_overflow=20,
                pool_pre_ping=True,
                pool_recycle=300,
                echo=False
            )
        
        # 接続テスト
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        
        logger.info(f"✅ Database engine created successfully ({'SQLite' if is_sqlite else 'PostgreSQL'})")
        return engine
        
    except Exception as e:
        logger.error(f"❌ Failed to create database engine: {e}")
        raise

# ===== 改善版: Groq初期化（接続テストを安全に実行） =====
def initialize_groq_client():
    """Groqクライアントを初期化し、接続テストを実行"""
    global groq_client
    
    try:
        from groq import Groq
        
        if not GROQ_API_KEY or GROQ_API_KEY == 'DUMMY_GROQ_KEY':
            logger.warning("⚠️ GROQ_API_KEY is not set - AI features will be disabled.")
            return None
            
        if len(GROQ_API_KEY) < 20:
            logger.error(f"❌ GROQ_API_KEY is too short (length: {len(GROQ_API_KEY)})")
            return None
        
        client = Groq(api_key=GROQ_API_KEY.strip())
        
        # 接続テストはスキップ（起動時間短縮のため）
        logger.info("✅ Groq client initialized (connection test skipped for faster startup).")
        return client
            
    except ImportError as e:
        logger.error(f"❌ Failed to import Groq library: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Groq client initialization failed: {e}")
        return None
        
# --- ユーティリティ関数 ---
def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def get_japan_time():
    now = datetime.now(timezone(timedelta(hours=9)))
    return f"今は{now.year}年{now.month}月{now.day}日の{now.hour}時{now.minute}分だよ！"

def create_news_hash(title, content):
    return hashlib.md5(f"{title}{content[:100]}".encode('utf-8')).hexdigest()

def is_time_request(message):
    return any(keyword in message for keyword in ['今何時', '時間', '時刻', '何時', 'なんじ'])

def is_weather_request(message):
    return any(keyword in message for keyword in ['天気', 'てんき', '気温', '雨', '晴れ', '曇り', '雪'])

def is_recommendation_request(message):
    return any(keyword in message for keyword in ['おすすめ', 'オススメ', '推薦', '紹介して'])

def detect_specialized_topic(message):
    message_normalized = unicodedata.normalize('NFKC', message).lower()
    for topic, config in SPECIALIZED_SITES.items():
        for keyword in config['keywords']:
            keyword_normalized = unicodedata.normalize('NFKC', keyword).lower()
            if keyword_normalized in message_normalized:
                logger.info(f"🎯 Specialized topic detected: {topic} (Keyword: {keyword})")
                return topic
    return None

def is_detailed_request(message):
    return any(keyword in message for keyword in ['詳しく', '詳細', 'くわしく', '教えて', '説明して', '解説して', 'どういう', 'なぜ', 'どうして', '理由', '原因', '具体的に'])

def is_explicit_search_request(message):
    """「調べて」「検索して」など、明確な検索意図のキーワードを検出する"""
    return any(keyword in message for keyword in ['調べて', '検索して', '探して', 'WEB検索', 'ググって'])

def should_search(message):
    """検索が必要かを判定（短い相槌や明示的な検索指示は除外）"""
    # ★ 最優先: 短い相槌や明示的な検索はここでは判定しない
    if is_short_response(message) or is_explicit_search_request(message):
        return False
    
    # 専門トピック検出
    if detect_specialized_topic(message):
        return True
    
    # ホロライブ関連（ニュース以外）
    # ホロメンの名前と具体的な質問が含まれている場合は検索対象
    for member_name in HOLOMEM_KEYWORDS:
        if member_name in message:
            # 「ニュース」「最新」などのキーワードがないことを確認
            if not any(kw in message for kw in ['ニュース', '最新', '情報', 'お知らせ']):
                # メンバー名以外の具体的な質問があるか簡易的に判定
                # 例：「さくらみこのマイクラは？」のような質問
                if len(message.replace(member_name, '').strip()) > 5: # メンバー名以外の部分が5文字以上なら具体的とみなす
                     return True
    
    # おすすめリクエスト
    if is_recommendation_request(message):
        return True
    
    # 明確な検索パターン
    search_patterns = [
        r'(?:とは|について|教えて|説明して|解説して)',
        r'(?:誰ですか|何ですか|どこですか|いつですか|なぜですか|どうして)'
    ]
    if any(re.search(pattern, message) for pattern in search_patterns):
        return True
    
    return False

def is_story_request(message):
    return any(keyword in message for keyword in ['面白い話', 'おもしろい話', '話して', '雑談', 'ネタ', '何か話', '喋って'])

def is_emotional_expression(message):
    emotional_keywords = {
        '眠': ['眠たい', '眠い', 'ねむい'], '疲': ['疲れた', 'つかれた'], '嬉': ['嬉しい', 'うれしい'],
        '楽': ['楽しい', 'たのしい'], '悲': ['悲しい', 'かなしい'], '寂': ['寂しい', 'さびしい'],
        '怒': ['怒', 'むかつく', 'イライラ'], '暇': ['暇', 'ひま']
    }
    for key, keywords in emotional_keywords.items():
        if any(kw in message for kw in keywords): return key
    return None

def is_seasonal_topic(message):
    return any(keyword in message for keyword in ['お月見', '花見', '紅葉', 'クリスマス', '正月', 'ハロウィン'])

def is_short_response(message):
    """短い相槌・返事を判定（検索対象外にする）"""
    msg = message.strip()
    
    # 3文字以下
    if len(msg) <= 3:
        return True
    
    # 典型的な相槌パターン
    short_responses = [
        'うん', 'そう', 'はい', 'そっか', 'なるほど', 'ふーん', 'へー',
        'そうなんだ', 'へぇ', 'ほう', 'あー', 'おー', 'ふむ',
        '何言ってたかな', 'どうだったかな', 'なんだったかな',
        '忘れた', '覚えてない', 'わからない'
    ]
    
    if msg in short_responses:
        return True
    
    # 「〜かな」で終わる短い発言（10文字以内）
    if len(msg) <= 10 and msg.endswith('かな'):
        return True
    
    return False

def is_news_detail_request(message):
    match = re.search(r'([1-9]|[１-９])番|【([1-9]|[１-９])】', message)
    if match and any(keyword in message for keyword in ['詳しく', '詳細', '教えて', 'もっと']):
        number_str = next(filter(None, match.groups()))
        return int(unicodedata.normalize('NFKC', number_str))
    return None

def is_friend_request(message):
    return any(fk in message for fk in ['友だち', '友達', 'フレンド']) and any(ak in message for ak in ['登録', '教えて', '誰', 'リスト'])

# ↓↓↓ ここに追加 ↓↓↓
def limit_text_for_sl(text, max_length=SL_SAFE_CHAR_LIMIT):
    """
    テキストを指定文字数以内に制限
    - 制限内ならそのまま返す
    - 超えている場合は切り詰めて「...」を追加
    """
    if len(text) <= max_length:
        return text
    
    # 単純に切り詰め
    return text[:max_length - 3] + "..."
    
def extract_location(message):
    for location in LOCATION_CODES.keys():
        if location in message:
            return location
    return "東京"

# --- ニュースキャッシュ管理 ---
def save_news_cache(session, user_uuid, news_items, news_type='hololive'):
    try:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        session.query(NewsCache).filter(NewsCache.user_uuid == user_uuid, NewsCache.created_at < one_hour_ago).delete()
        for i, news in enumerate(news_items, 1):
            cache = NewsCache(user_uuid=user_uuid, news_id=news.id, news_number=i, news_type=news_type)
            session.add(cache)
        session.commit()
        logger.info(f"💾 News cache saved for user {user_uuid}: {len(news_items)} items.")
    except Exception as e:
        logger.error(f"Error saving news cache: {e}")
        session.rollback()

def get_cached_news_detail(session, user_uuid, news_number):
    try:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        cache = session.query(NewsCache).filter(
            NewsCache.user_uuid == user_uuid,
            NewsCache.news_number == news_number,
            NewsCache.created_at > one_hour_ago
        ).order_by(NewsCache.created_at.desc()).first()
        if not cache: return None
        
        NewsModel = HololiveNews if cache.news_type == 'hololive' else SpecializedNews
        return session.query(NewsModel).filter_by(id=cache.news_id).first()
    except Exception as e:
        logger.error(f"Error getting cached news detail: {e}")
        return None

# --- コア機能: 天気, ニュース, Wiki, 友達 ---
def get_weather_forecast(location):
    """天気予報取得（文字数制限版）"""
    area_code = LOCATION_CODES.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        text = clean_text(response.json().get('text', ''))
        
        if not text:
            return f"{location}の天気情報がちょっと取れなかった…"
        
        # ★ 150文字以内に制限
        weather_text = f"今の{location}の天気はね、「{text}」って感じだよ！"
        return limit_text_for_sl(weather_text, 150)
        
    except Exception as e:
        logger.error(f"Weather API error for {location}: {e}")
        return "天気情報がうまく取れなかったみたい…"
# ===== 改善版: 記事取得（リトライ機構付き） =====
def fetch_article_content(article_url, max_retries=3, timeout=15):
    """記事コンテンツを取得（リトライ機構付き）"""
    for attempt in range(max_retries):
        try:
            response = requests.get(
                article_url,
                headers={'User-Agent': random.choice(USER_AGENTS)},
                timeout=timeout,
                allow_redirects=True
            )
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # より多くのセレクタパターンを試行
            content_selectors = [
                'article .entry-content',
                '.post-content',
                '.article-content',
                'article',
                '.content',
                'main article',
                '[class*="post-body"]',
                '[class*="entry"]'
            ]
            
            content_elem = None
            for selector in content_selectors:
                content_elem = soup.select_one(selector)
                if content_elem:
                    break
            
            if content_elem:
                paragraphs = content_elem.find_all('p')
                article_text = ' '.join([
                    clean_text(p.get_text()) 
                    for p in paragraphs 
                    if len(clean_text(p.get_text())) > 20
                ])
                
                if article_text:
                    return article_text[:2000]
            
            # フォールバック: メタディスクリプションを取得
            meta_desc = soup.find('meta', {'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                return clean_text(meta_desc['content'])
            
            return None
            
        except requests.exceptions.Timeout:
            logger.warning(f"⚠️ Timeout on attempt {attempt + 1}/{max_retries} for {article_url}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            continue
            
        except Exception as e:
            logger.warning(f"⚠️ Article fetching error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            continue
    
    logger.error(f"❌ Failed to fetch article after {max_retries} attempts: {article_url}")
    return None

def summarize_article(title, content):
    if not groq_client or not content: return content[:500] if content else title
    try:
        prompt = f"以下のニュース記事を200文字以内で簡潔に要約してください。\n\nタイトル: {title}\n本文: {content[:1500]}\n\n要約:"
        completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", temperature=0.5, max_tokens=200)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Summarization error: {e}")
        return content[:500] if content else title

def _update_news_database(session, model, site_name, base_url, selectors):
    added_count = 0
    try:
        response = requests.get(base_url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15, allow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        articles_found = []
        for selector in selectors:
            found = soup.select(selector)
            if found:
                articles_found = found[:10]
                break
        
        for article in articles_found[:5]:
            title_elem = article.find(['h1', 'h2', 'h3', 'a'])
            if not title_elem: continue
            title = clean_text(title_elem.get_text())
            link_elem = title_elem if title_elem.name == 'a' else article.find('a', href=True)
            if not title or len(title) < 5 or not link_elem: continue
            
            article_url = urljoin(base_url, link_elem.get('href', ''))
            article_content = fetch_article_content(article_url) or title
            news_hash = create_news_hash(title, article_content)
            
            if not session.query(model).filter_by(news_hash=news_hash).first():
                summary = summarize_article(title, article_content)
                new_news_data = {'title': title, 'content': summary, 'news_hash': news_hash, 'url': article_url}
                if model == SpecializedNews: new_news_data['site_name'] = site_name
                session.add(model(**new_news_data))
                added_count += 1
                logger.info(f"➕ New article added for {site_name}: {title[:50]}")
                if groq_client: time.sleep(0.5)

        if added_count > 0: session.commit()
        logger.info(f"✅ {site_name} DB update complete: {added_count} new articles.")
    except Exception as e:
        logger.error(f"❌ {site_name} news update error: {e}")
        session.rollback()

def update_hololive_news_database():
    session = Session()
    _update_news_database(session, HololiveNews, "Hololive", HOLOLIVE_NEWS_URL, ['article', '.post', '.entry', '[class*="post"]', '[class*="article"]'])
    session.close()

def update_all_specialized_news():
    for site_name, config in SPECIALIZED_SITES.items():
        # 「セカンドライフ」は定期巡回の対象外とする
        if site_name == 'セカンドライフ':
            logger.info("ℹ️ Skipping proactive scraping for 'セカンドライフ' as per policy.")
            continue  # ループの次の要素へ進む

        session = Session()
        _update_news_database(session, SpecializedNews, site_name, config['base_url'], ['article', '.post', '.entry', '[class*="post"]', '[class*="article"]'])
        session.close()
        time.sleep(2)


# ===== 改善版: さくらみこ専用の情報拡張 =====
def get_sakuramiko_special_responses():
    """さくらみこに関する特別な応答パターン"""
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね!あの独特な口癖がエリートの証なんだって〜',
        'エリート': 'みこちは自称エリートVTuber!でも実際は愛されポンコツキャラって感じで、それがまた魅力的なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてるの知ってる?',
        'FAQ': 'みこちのFAQ(Frequently Asked Questions)、実は本人が答えるんじゃなくてファンが質問するコーナーなんだよ〜面白いでしょ?',
        'GTA': 'みこちのGTA配信、カオスで最高!警察に追われたり、変なことしたり、見てて飽きないんだよね〜'
    }

# ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼【ここからが変更箇所です】▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
def get_holomem_info(member_name):
    """ホロメンの情報をDBから取得する"""
    session = Session()
    try:
        wiki = session.query(HolomemWiki).filter_by(member_name=member_name).first()
        if wiki:
            # データベースの全ての情報を辞書として返す
            info = {
                'name': wiki.member_name, 
                'description': wiki.description, 
                'debut_date': wiki.debut_date, 
                'generation': wiki.generation, 
                'tags': json.loads(wiki.tags) if wiki.tags else [],
                'graduation_date': wiki.graduation_date,
                'graduation_reason': wiki.graduation_reason,
                'mochiko_feeling': wiki.mochiko_feeling
            }
            return info
        return None
    except Exception as e:
        logger.error(f"Error getting holomem info for {member_name}: {e}")
        return None
    finally:
        session.close()
# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲【ここまでが変更箇所です】▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲

def register_friend(user_uuid, friend_uuid, friend_name, relationship_note=""):
    session = Session()
    try:
        if session.query(FriendRegistration).filter_by(user_uuid=user_uuid, friend_uuid=friend_uuid).first():
            return False
        session.add(FriendRegistration(user_uuid=user_uuid, friend_uuid=friend_uuid, friend_name=friend_name, relationship_note=relationship_note))
        session.commit()
        return True
    except Exception as e:
        logger.error(f"❌ Friend registration error: {e}")
        session.rollback()
        return False
    finally:
        session.close()

def get_friend_list(user_uuid):
    session = Session()
    try:
        friends = session.query(FriendRegistration).filter_by(user_uuid=user_uuid).order_by(FriendRegistration.registered_at.desc()).all()
        return [{'name': f.friend_name, 'uuid': f.friend_uuid, 'note': f.relationship_note} for f in friends]
    finally:
        session.close()

def generate_voice(text, speaker_id=VOICEVOX_SPEAKER_ID):
    """音声生成（改善版）"""
    if not VOICEVOX_ENABLED:
        logger.warning("⚠️ VOICEVOX is disabled")
        return None
    
    # ディレクトリの存在を確認（毎回チェック）
    if not os.path.exists(VOICE_DIR):
        logger.warning(f"⚠️ Voice directory missing, recreating: {VOICE_DIR}")
        ensure_voice_directory()
    
    voicevox_url = VOICEVOX_URL_FROM_ENV or "http://localhost:50021"
    
    try:
        # Step 1: クエリ作成
        logger.info(f"🎤 Generating voice query for: {text[:50]}...")
        query_response = requests.post(
            f"{voicevox_url}/audio_query",
            params={"text": text, "speaker": speaker_id},
            timeout=10
        )
        query_response.raise_for_status()
        
        # Step 2: 音声合成
        logger.info(f"🎵 Synthesizing voice...")
        synthesis_response = requests.post(
            f"{voicevox_url}/synthesis",
            params={"speaker": speaker_id},
            json=query_response.json(),
            timeout=30
        )
        synthesis_response.raise_for_status()
        
        # Step 3: ファイル保存
        timestamp = int(time.time())
        random_suffix = random.randint(1000, 9999)
        filename = f"voice_{timestamp}_{random_suffix}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        
        logger.info(f"💾 Saving voice file: {filename}")
        with open(filepath, 'wb') as f:
            f.write(synthesis_response.content)
        
        # ファイルサイズ確認
        file_size = os.path.getsize(filepath)
        logger.info(f"✅ Voice generated successfully: {filename} ({file_size} bytes)")
        
        return filepath
        
    except requests.exceptions.Timeout:
        logger.error(f"❌ VOICEVOX timeout: {voicevox_url}")
        return None
    except requests.exceptions.ConnectionError as e:
        logger.error(f"❌ VOICEVOX connection error: {e}")
        return None
    except OSError as e:
        logger.error(f"❌ File system error: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ VOICEVOX voice generation error: {e}")
        return None

def cleanup_old_data_advanced():
    session = Session()
    try:
        three_months_ago = datetime.utcnow() - timedelta(days=90)
        deleted_conversations = session.query(ConversationHistory).filter(ConversationHistory.timestamp < three_months_ago).delete()
        deleted_holo_news = session.query(HololiveNews).filter(HololiveNews.created_at < three_months_ago).delete()
        deleted_specialized_news = session.query(SpecializedNews).filter(SpecializedNews.created_at < three_months_ago).delete()
        
        one_day_ago = datetime.utcnow() - timedelta(days=1)
        deleted_tasks = session.query(BackgroundTask).filter(BackgroundTask.status == 'completed', BackgroundTask.completed_at < one_day_ago).delete()
        
        session.commit()
        if any([deleted_conversations, deleted_holo_news, deleted_specialized_news, deleted_tasks]):
            logger.info(f"🧹 Data cleanup complete. Deleted: {deleted_conversations} convos, {deleted_holo_news + deleted_specialized_news} news, {deleted_tasks} tasks.")
    except Exception as e:
        logger.error(f"Data cleanup error: {e}")
        session.rollback()
    finally:
        session.close()

# --- Web検索機能 ---
def scrape_major_search_engines(query, num_results):
    search_configs = [
        {'name': 'Bing', 'url': f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP", 'result_selector': 'li.b_algo', 'title_selector': 'h2', 'snippet_selector': 'div.b_caption p, .b_caption'},
        {'name': 'Yahoo Japan', 'url': f"https://search.yahoo.co.jp/search?p={quote_plus(query)}", 'result_selector': 'div.Algo', 'title_selector': 'h3', 'snippet_selector': 'div.compText p, .compText'}
    ]
    for config in search_configs:
        try:
            logger.info(f"🔍 Searching on {config['name']} for: {query}")
            response = requests.get(config['url'], headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=12)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            for elem in soup.select(config['result_selector'])[:num_results]:
                title = elem.select_one(config['title_selector'])
                snippet = elem.select_one(config['snippet_selector'])
                if title and snippet and len(clean_text(title.get_text())) > 3:
                    results.append({'title': clean_text(title.get_text())[:200], 'snippet': clean_text(snippet.get_text())[:300]})
            if results: return results
        except Exception as e:
            logger.warning(f"⚠️ {config['name']} search error: {e}")
    return []

def deep_web_search(query, is_detailed):
    logger.info(f"🔍 Starting deep web search (Detailed: {is_detailed})")
    results = scrape_major_search_engines(query, 3 if is_detailed else 2)
    if not results: return None
    
    summary_text = "\n".join(f"[情報{i+1}] {res['snippet']}" for i, res in enumerate(results))
    if not groq_client: return f"検索結果:\n{summary_text}"
    
    try:
        prompt = f"""以下の検索結果を使い、質問「{query}」にギャル語で、{'詳しく' if is_detailed else '簡潔に'}答えて：
検索結果:\n{summary_text}\n\n回答の注意点:\n- 一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。\n- {'400文字程度で詳しく' if is_detailed else '200文字以内で簡潔に'}。"""
        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant",
            temperature=0.7, max_tokens=400 if is_detailed else 200
        )
        ai_response = completion.choices[0].message.content.strip()
        return ai_response if len(ai_response) > 50 else f"検索結果:\n{summary_text}"
    except Exception as e:
        logger.error(f"AI summarization error: {e}")
        return f"検索結果:\n{summary_text}"

# --- AI応答 & フォールバック ---
def generate_fallback_response(message, reference_info=""):
    """フォールバック応答（自然な会話重視）"""
    if reference_info:
        return f"調べてきたよ！\n\n{reference_info[:500]}"
    
    # 時間・天気は専用処理
    if is_time_request(message):
        return get_japan_time()
    if is_weather_request(message):
        return get_weather_forecast(extract_location(message))
    
    # 挨拶パターン
    greetings = {
        'こんにちは': ['やっほー！', 'こんにちは〜！元気？'],
        'おはよう': ['おはよ〜！今日もいい天気だね！', 'おっはよ〜！'],
        'こんばんは': ['こんばんは！今日どうだった？', 'ばんは〜！', 'こんもち～'],
        'ありがとう': ['どういたしまして！', 'いえいえ〜！'],
        'おやすみ': ['おやすみ〜！また明日ね！', 'いい夢見てね〜！'],
        '疲れた': ['お疲れさま！ゆっくり休んでね！', '無理しないでね！'],
        '暇': ['暇なんだ〜！何か話そっか？', 'じゃあホロライブの話する？'],
        '元気': ['元気だよ〜！あなたは？', 'まじ元気！ありがと！'],
        '好き': ['うける！ありがと〜！', 'まじで？惚れてまうやろ！'],
        'かわいい': ['ありがと！照れるじゃん！', 'まじで？うれしー！', '当然じゃん！'],
        'すごい': ['うける！', 'でしょ？まじうれしい！'],
    }
    
    for keyword, responses in greetings.items():
        if keyword in message:
            return random.choice(responses)
    
    # 感情表現への共感
    emotions = {
        '眠': ['眠いんだ〜。早く寝たほうがいいよ！', '無理しないでね〜'],
        '嬉': ['それは良かったね！まじ嬉しい！', 'やった〜！あてぃしも嬉しい！'],
        '楽': ['楽しそう！何してるの？', 'いいね〜！まじ楽しそう！'],
        '悲': ['大丈夫？何かあった？', '元気出してね…'],
        '寂': ['寂しいの？話そうよ！', 'あてぃしがいるじゃん！'],
        '怒': ['何があったの？聞くよ？', 'イライラするよね…わかる'],
    }
    
    for key, responses in emotions.items():
        if key in message:
            return random.choice(responses)
    
    # 質問パターン
    if '?' in message or '？' in message:
        return random.choice([
            "それ、気になるね！もっと教えて？",
            "うーん、難しいけど考えてみるよ！",
            "それについては、もうちょっと詳しく聞いてもいい？"
        ])
    
    # デフォルト: 相槌
    return random.choice([
        "うんうん、聞いてるよ！",
        "なるほどね！",
        "そうなんだ！面白いね！",
        "まじで？もっと話して！",
        "へぇ〜！それでそれで？",
        "わかるわかる！",
    ])



# ========================================
# 【修正2】generate_ai_response - もちこの性格を明確化
# ========================================

def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    """AI応答生成（性格設定改善版）"""
    if not groq_client:
        logger.warning("⚠️ Groq client not available, using fallback")
        return generate_fallback_response(message, reference_info)
    
    try:
        # Step 1: 心理分析結果を取得
        user_uuid = user_data.get('uuid')
        psychology = None
        if user_uuid:
            psychology = get_user_psychology(user_uuid)
        
        is_hololive_topic = is_hololive_request(message)
        
        # ★ 修正: シンプルで明確な性格設定
        system_prompt_parts = [
            f"あなたは「もちこ」という22歳のギャルAIです。{user_data['name']}さんと楽しく話しています。",
            "",
            "# 🎀 もちこの基本設定",
            "- **一人称**: 「あてぃし」",
            "- **語尾**: 「〜じゃん」「〜的な？」「〜だよね」",
            "- **口癖**: 「まじ」「てか」「うける」「やば」「ぴえん」",
            "- **性格**: 明るい、フレンドリー、ちょっとおっちょこちょい",
            "",
            "# 💬 会話スタイル",
            "1. **短く、テンポよく**（100-150文字が基本）",
            "2. **共感重視**: 相手の気持ちに寄り添う",
            "3. **自然体**: 無理に話題を変えない",
            "4. **ギャル語**: でも読みやすさも大事",
            "",
        ]
        
        # Step 2: 心理プロファイル活用（簡潔版）
        if psychology and psychology['confidence'] > 60:
            system_prompt_parts.extend([
                f"# 🧠 {user_data['name']}さんの特徴",
                f"- {psychology['conversation_style']}な人",
                f"- {psychology['emotional_tendency']}タイプ",
                f"- よく話す話題: {', '.join(psychology['favorite_topics'][:3])}",
                "→ この人に合わせた話し方を意識してね！",
                "",
            ])
        
        # Step 3: ホロライブモード（明確化）
        if is_hololive_topic:
            system_prompt_parts.extend([
                "# 🌟 【ホロライブモード発動中】",
                "- ホロライブの話が出たので、詳しく教えてあげて！",
                "- もちこもホロライブ大好きだから熱く語ってOK",
                "",
            ])
        else:
            system_prompt_parts.extend([
                "# ⚠️ ホロライブについて",
                "- **相手から話題に出ない限り、自分から話さない**",
                "- 参考情報がホロライブと無関係なら絶対に混ぜない",
                "",
            ])
        
        # Step 4: タスク報告モード
        if is_task_report:
            system_prompt_parts.extend([
                "# 📢 【検索結果報告モード】",
                "**やること:**",
                f"1. まず「おまたせ！{completed_task['query']}の件だけど…」と言う",
                "2. 【参考情報】を**要約して**わかりやすく伝える",
                "3. **参考情報にないことは絶対に追加しない**",
                "4. その後、現在の発言にも自然に反応する",
                "",
            ])
        
        # Step 5: 詳細説明モード
        if is_detailed:
            system_prompt_parts.extend([
                "# 📚 【詳細説明モード】",
                "- 400文字程度でしっかり説明",
                "- 【参考情報】を最大限活用",
                "",
            ])
        
        # Step 6: 参考情報の追加
        if reference_info:
            system_prompt_parts.extend([
                "## 【参考情報】",
                reference_info,
                "",
                "↑この情報を使って答えてね！",
            ])
        
        system_prompt = "\n".join(system_prompt_parts)
        
        # メッセージ構築
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend([{"role": h.role, "content": h.content} for h in reversed(history)])
        messages.append({"role": "user", "content": message})
        
        logger.info(f"🤖 Generating AI response (Hololive: {is_hololive_topic}, Detailed: {is_detailed})")
        
        # AI応答生成
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            temperature=0.8,  # ★ 少し高めでギャル感アップ
            max_tokens=500 if is_detailed or is_task_report else 150,
            top_p=0.9
        )
        
        response = completion.choices[0].message.content.strip()
        
        # ★ 修正: 文字数制限を適用
        if not is_detailed:
            response = limit_text_for_sl(response, 150)
        
        logger.info(f"✅ AI response: {response[:80]}")
        
        return response
        
    except Exception as e:
        logger.error(f"❌ AI response generation error: {e}", exc_info=True)
        return generate_fallback_response(message, reference_info)

# ========================================
# 【修正3】追加質問対応 - 検索結果への深掘り質問を検出
# ========================================

def is_follow_up_question(message, history):
    """直前の回答に対する追加質問かどうか判定"""
    if not history or len(history) < 2:
        return False
    
    # 最新のAI応答を取得
    last_assistant_msg = None
    for h in history:
        if h.role == 'assistant':
            last_assistant_msg = h.content
            break
    
    if not last_assistant_msg:
        return False
    
    # 追加質問のパターン
    follow_up_patterns = [
        r'もっと(?:詳しく|くわしく)',
        r'(?:それ|これ)(?:について|って)?(?:詳しく|くわしく)',
        r'(?:なぜ|どうして|なんで)',
        r'(?:どういう|どんな)(?:こと|意味|感じ)',
        r'(?:例えば|たとえば)',
        r'(?:具体的|ぐたいてき)には?',
        r'(?:他|ほか)には?',
        r'(?:続き|つづき)',
        r'(?:もう少し教えて|もうちょっと教えて)',
    ]
    
    for pattern in follow_up_patterns:
        if re.search(pattern, message):
            logger.info(f"🔍 Follow-up question detected: {pattern}")
            return True
    
    return False

# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲【ここまでが変更箇所です】▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲


# --- ユーザー & バックグラウンドタスク管理 ---


def get_conversation_history(session, uuid):
    return session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(4).all()

def check_completed_tasks(user_uuid):
    session = Session()
    try:
        task = session.query(BackgroundTask).filter_by(user_uuid=user_uuid, status='completed').order_by(BackgroundTask.completed_at.desc()).first()
        if task:
            result = {'query': task.query, 'result': task.result}
            session.delete(task)
            session.commit()
            return result
    finally:
        session.close()
    return None

def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name: user.user_name = name
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
    session.add(user)
    session.commit()
    # ★ 修正: uuidを含める
    return {'name': user.user_name, 'uuid': uuid}


# ===== 【修正】background_deep_search 関数 =====
# アニメ検索を追加

def background_deep_search(task_id, query, is_detailed):
    """バックグラウンド検索（アニメ対応版）"""
    session = Session()
    search_result = None
    
    logger.info(f"🔍 Background search started (Task ID: {task_id}, Query: '{query}')")
    
    try:
        # Step 1: アニメリクエストの判定
        if is_anime_request(query):
            logger.info(f"🎬 Anime query detected: {query}")
            anime_result = search_anime_database(query, is_detailed)
            
            if anime_result:
                search_result = f"アニメデータベースからの情報:\n\n{anime_result}"
            else:
                # アニメDBで見つからない場合は通常検索
                search_result = deep_web_search(f"アニメ {query}", is_detailed)
        
        # Step 2: 専門トピック検出（既存のロジック）
        elif (specialized_topic := detect_specialized_topic(query)):
            logger.info(f"🎯 Specialized topic detected: {specialized_topic}")
            
            if specialized_topic == 'セカンドライフ':
                search_result = deep_web_search(f"Second Life 最新情報 {query}", is_detailed)
            else:
                news_items = session.query(SpecializedNews).filter(
                    SpecializedNews.site_name == specialized_topic
                ).order_by(SpecializedNews.created_at.desc()).limit(3).all()
                
                if news_items:
                    search_result = f"{specialized_topic}のデータベース情報:\n" + "\n".join(
                        f"・{n.title}: {n.content[:150]}" for n in news_items
                    )
                else:
                    search_result = deep_web_search(
                        f"site:{SPECIALIZED_SITES[specialized_topic]['base_url']} {query}",
                        is_detailed
                    )
        
        # Step 3: ホロメン検索（既存のロジック）
        elif any(member in query for member in HOLOMEM_KEYWORDS):
            holomem_matched = None
            query_topic = ""
            for member_name in HOLOMEM_KEYWORDS:
                if member_name in query:
                    holomem_matched = member_name
                    query_topic = query.replace(member_name, '').replace('について', '').replace('教えて', '').strip()
                    if not query_topic:
                        query_topic = "概要"
                    break
            
            wiki_info = get_holomem_info(holomem_matched)
            if wiki_info and query_topic == "概要":
                search_result = f"{holomem_matched}に関するデータベース情報:\n{wiki_info['description']}"
            else:
                search_result = deep_web_search(f"ホロライブ {holomem_matched} {query_topic}", is_detailed)
        
        # Step 4: 通常検索
        else:
            logger.info("🌐 General web search")
            search_result = deep_web_search(query, is_detailed)
        
        # Step 5: 結果の検証
        if not search_result or len(search_result.strip()) < 10:
            logger.warning(f"⚠️ Search result too short or empty for: {query}")
            search_result = f"「{query}」について調べたんだけど、まじで情報が見つからなかったよ…！別の聞き方で試してみて？"
        
        logger.info(f"✅ Search completed: {len(search_result)} chars")
        
    except Exception as e:
        logger.error(f"❌ Background search error for '{query}': {e}", exc_info=True)
        search_result = f"検索中にエラーが発生しちゃった…！「{query}」についてもう一回聞いてみて？"
    
    finally:
        # タスク結果を保存
        try:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = search_result
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
                session.commit()
                logger.info(f"💾 Task {task_id} saved successfully")
        except Exception as e:
            logger.error(f"❌ Failed to save task result: {e}")
            session.rollback()
        finally:
            session.close()

def start_background_search(user_uuid, query, is_detailed):
    task_id = str(uuid.uuid4())[:8]
    session = Session()
    try:
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=query)
        session.add(task)
        session.commit()
        background_executor.submit(background_deep_search, task_id, query, is_detailed)
        return task_id
    except Exception as e:
        logger.error(f"❌ Background task creation error: {e}")
        session.rollback()
        return None
    finally:
        session.close()

# ========================================
# 【追加1】データベース修正リクエストを検出する関数
# ========================================

def detect_db_correction_request(message):
    """
    ユーザーがDB情報の誤りを指摘しているか判定
    
    Returns:
        dict or None: {
            'type': 'holomem_correction',  # 修正タイプ
            'member_name': 'さくらみこ',
            'correction_type': 'graduation',  # graduation, debut_date, generation, description
            'user_claim': '2024年1月に卒業した'  # ユーザーの主張
        }
    """
    
    # パターン1: 「〜は間違ってる」「〜は違う」
    correction_patterns = [
        r'(.+?)(?:は|が)(?:間違[いっ]てる|違う|誤[りっ]てる)',
        r'(.+?)(?:じゃない|ではない)',
        r'実は(.+?)(?:だよ|です|なんだ)',
        r'正しくは(.+?)(?:だよ|です)',
    ]
    
    for pattern in correction_patterns:
        match = re.search(pattern, message)
        if match:
            logger.info(f"🔍 Potential DB correction detected: {message}")
            
            # ホロメン名を抽出
            holomem_keywords = get_active_holomem_keywords()
            member_name = None
            
            for keyword in holomem_keywords:
                if keyword in message:
                    member_name = keyword
                    break
            
            if not member_name:
                return None
            
            # 修正内容を判定
            correction_type = None
            user_claim = match.group(1).strip()
            
            if any(kw in message for kw in ['卒業', '引退', 'やめた', '辞めた']):
                correction_type = 'graduation'
            elif any(kw in message for kw in ['デビュー', 'debut', '始めた', '活動開始']):
                correction_type = 'debut_date'
            elif any(kw in message for kw in ['期生', '世代', 'generation']):
                correction_type = 'generation'
            else:
                correction_type = 'description'
            
            return {
                'type': 'holomem_correction',
                'member_name': member_name,
                'correction_type': correction_type,
                'user_claim': user_claim,
                'original_message': message
            }
    
    return None


# ========================================
# 【追加2】事実確認用のWEB検索関数
# ========================================

def verify_and_correct_holomem_info(correction_request):
    """
    ユーザーの指摘内容をWEB検索で検証し、正しければDBを修正
    
    Args:
        correction_request: detect_db_correction_request() の戻り値
        
    Returns:
        dict: {
            'verified': True/False,
            'correction_made': True/False,
            'search_result': '検索結果テキスト',
            'updated_info': {...}  # 更新後の情報
        }
    """
    member_name = correction_request['member_name']
    correction_type = correction_request['correction_type']
    user_claim = correction_request['user_claim']
    
    logger.info(f"🔍 Verifying correction for {member_name}: {correction_type}")
    
    # Step 1: 検索クエリを生成
    search_queries = {
        'graduation': f"ホロライブ {member_name} 卒業 引退 いつ",
        'debut_date': f"ホロライブ {member_name} デビュー日 活動開始",
        'generation': f"ホロライブ {member_name} 何期生",
        'description': f"ホロライブ {member_name} プロフィール"
    }
    
    query = search_queries.get(correction_type, f"ホロライブ {member_name}")
    
    # Step 2: WEB検索実行
    search_results = scrape_major_search_engines(query, 5)
    
    if not search_results:
        logger.warning(f"⚠️ No search results for verification: {query}")
        return {
            'verified': False,
            'correction_made': False,
            'search_result': '検索結果が見つかりませんでした。',
            'message': f"ごめん、{member_name}ちゃんの情報を確認できなかったよ…"
        }
    
    # Step 3: 検索結果を統合
    combined_results = "\n".join([
        f"[情報{i+1}] タイトル: {r['title']}\n内容: {r['snippet']}"
        for i, r in enumerate(search_results)
    ])
    
    # Step 4: AIで事実確認
    if not groq_client:
        logger.error("❌ Groq client unavailable for verification")
        return {
            'verified': False,
            'correction_made': False,
            'search_result': combined_results[:500],
            'message': 'AI検証機能が利用できません。'
        }
    
    try:
        verification_prompt = f"""あなたは事実確認の専門家です。以下の情報を検証してください。

【対象メンバー】{member_name}
【ユーザーの主張】{user_claim}
【検索結果】
{combined_results[:2000]}

【タスク】
1. ユーザーの主張が検索結果から**事実として確認できるか**判定
2. 確認できた場合、正確な情報を抽出

**重要**: 以下のJSON形式でのみ回答してください:
{{
  "verified": true/false,
  "confidence": 0-100,
  "extracted_info": {{
    "graduation_date": "YYYY年MM月DD日" or null,
    "graduation_reason": "理由" or null,
    "debut_date": "YYYY年MM月DD日" or null,
    "generation": "N期生" or null,
    "description": "説明文" or null
  }},
  "reasoning": "判定理由（50文字以内）"
}}

**判定基準**:
- 複数の信頼できる情報源で一致していればtrue
- 曖昧・矛盾・情報不足ならfalse
- confidence: 確信度（80以上で修正実行）"""

        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": verification_prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.2,  # 事実確認は低温度
            max_tokens=400
        )
        
        response_text = completion.choices[0].message.content.strip()
        
        # JSONを抽出
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(1)
        
        verification_result = json.loads(response_text)
        
        logger.info(f"✅ Verification result: {verification_result}")
        
        # Step 5: 信頼度が高ければDB修正
        if verification_result['verified'] and verification_result['confidence'] >= 80:
            session = Session()
            try:
                member = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                
                if not member:
                    logger.warning(f"⚠️ Member not found in DB: {member_name}")
                    return {
                        'verified': True,
                        'correction_made': False,
                        'search_result': combined_results[:500],
                        'message': f"情報は確認できたけど、{member_name}ちゃんがDBに登録されてないみたい…"
                    }
                
                # DB更新
                extracted = verification_result['extracted_info']
                updated_fields = []
                
                if extracted.get('graduation_date'):
                    member.graduation_date = extracted['graduation_date']
                    member.is_active = False
                    updated_fields.append('卒業日')
                
                if extracted.get('graduation_reason'):
                    member.graduation_reason = extracted['graduation_reason']
                    updated_fields.append('卒業理由')
                
                if extracted.get('debut_date'):
                    member.debut_date = extracted['debut_date']
                    updated_fields.append('デビュー日')
                
                if extracted.get('generation'):
                    member.generation = extracted['generation']
                    updated_fields.append('期生')
                
                if extracted.get('description'):
                    member.description = extracted['description']
                    updated_fields.append('プロフィール')
                
                member.last_updated = datetime.utcnow()
                
                session.commit()
                
                logger.info(f"✅ DB corrected for {member_name}: {', '.join(updated_fields)}")
                
                return {
                    'verified': True,
                    'correction_made': True,
                    'search_result': combined_results[:500],
                    'updated_fields': updated_fields,
                    'updated_info': extracted,
                    'message': f"調べてみたら本当だった！{member_name}ちゃんの情報を修正したよ！教えてくれてありがとう！✨"
                }
                
            except Exception as e:
                logger.error(f"❌ DB update error: {e}", exc_info=True)
                session.rollback()
                return {
                    'verified': True,
                    'correction_made': False,
                    'search_result': combined_results[:500],
                    'message': f"情報は正しかったんだけど、DB更新でエラーが出ちゃった…ごめん！"
                }
            finally:
                session.close()
        
        else:
            # 検証失敗
            logger.info(f"⚠️ Verification failed or low confidence: {verification_result['confidence']}%")
            return {
                'verified': False,
                'correction_made': False,
                'search_result': combined_results[:500],
                'confidence': verification_result['confidence'],
                'reasoning': verification_result['reasoning'],
                'message': f"うーん、調べてみたんだけど確証が持てなかった…（確信度{verification_result['confidence']}%）もうちょっと詳しい情報あったら教えて？"
            }
        
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON parse error in verification: {e}")
        logger.error(f"Raw response: {response_text[:500]}")
        return {
            'verified': False,
            'correction_made': False,
            'search_result': combined_results[:500],
            'message': 'AI検証でエラーが出ちゃった…'
        }
    except Exception as e:
        logger.error(f"❌ Verification error: {e}", exc_info=True)
        return {
            'verified': False,
            'correction_made': False,
            'search_result': combined_results[:500],
            'message': f"検証中にエラーが出ちゃった…ごめんね！"
        }


# ========================================
# 【追加3】バックグラウンドでDB修正を実行する関数
# ========================================

def background_db_correction(task_id, correction_request):
    """
    バックグラウンドでDB修正を実行
    """
    session = Session()
    
    try:
        logger.info(f"🔧 Starting DB correction (Task ID: {task_id})")
        
        # 検証 + 修正実行
        result = verify_and_correct_holomem_info(correction_request)
        
        # タスク結果を保存
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result['message']
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            
            # メタデータも保存
            task.query = json.dumps({
                'correction_type': correction_request['correction_type'],
                'member_name': correction_request['member_name'],
                'verified': result['verified'],
                'corrected': result['correction_made']
            }, ensure_ascii=False)
            
            session.commit()
            logger.info(f"✅ DB correction task completed: {task_id}")
    
    except Exception as e:
        logger.error(f"❌ Background DB correction error: {e}", exc_info=True)
        
        # エラーをタスクに記録
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = "DB修正中にエラーが発生しました。"
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
    
    finally:
        session.close()


def start_background_correction(user_uuid, correction_request):
    """DB修正タスクを開始"""
    task_id = str(uuid.uuid4())[:8]
    session = Session()
    
    try:
        task = BackgroundTask(
            task_id=task_id,
            user_uuid=user_uuid,
            task_type='db_correction',
            query=correction_request['original_message']
        )
        session.add(task)
        session.commit()
        
        background_executor.submit(background_db_correction, task_id, correction_request)
        
        logger.info(f"🚀 DB correction task started: {task_id}")
        return task_id
        
    except Exception as e:
        logger.error(f"❌ Background correction task creation error: {e}")
        session.rollback()
        return None
    finally:
        session.close()


# ========================================
# 【追加4】chat_lsl エンドポイントにDB修正検出を追加
# ========================================

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json
        user_uuid, user_name, message = data.get('uuid', ''), data.get('name', ''), data.get('message', '')
        
        if not all([user_uuid, user_name, message]):
            return "エラー: 必要な情報が足りないみたい…|", 400
        
        logger.info(f"💬 Received: {message} (from: {user_name})")
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        ai_text = ""
        
        # ★ 追加: DB修正リクエストの検出（最優先）
        correction_request = detect_db_correction_request(message)
        
        if correction_request:
            logger.info(f"🔧 DB correction request detected: {correction_request}")
            
            # バックグラウンドで検証 + 修正を開始
            if start_background_correction(user_uuid, correction_request):
                ai_text = f"え、まじで！？{correction_request['member_name']}ちゃんの情報、今すぐ調べて確認してみるね！ちょっと待ってて！"
            else:
                ai_text = "ごめん、今DB修正機能がうまく動いてないみたい…"
            
            # 会話履歴に保存
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
            session.commit()
            
            return f"{ai_text}|", 200
        
        # 以下、既存の処理（変更なし）
        
        # ★ 追加: 追加質問の判定
        is_follow_up = is_follow_up_question(message, history)
        
        # === 優先度1: ホロメン基本情報 ===
        holomem_keywords = get_active_holomem_keywords()
        basic_question_pattern = f"({'|'.join(re.escape(k) for k in holomem_keywords)})って(?:誰|だれ|何|なに)[\?？]?$"
        basic_question_match = re.search(basic_question_pattern, message.strip())
        
        if not ai_text and basic_question_match:
            member_name = basic_question_match.group(1)
            
            if member_name in ['ホロライブ', 'hololive', 'ホロメン']:
                ai_text = "ホロライブは、カバー株式会社が運営してるVTuber事務所のことだよ！ときのそらちゃんとか、たくさんの人気VTuberが所属してて、配信とかまじで楽しいからおすすめ！"
            else:
                wiki_info = get_holomem_info(member_name)
                if wiki_info:
                    response_parts = [f"{wiki_info['name']}ちゃんはね、ホロライブ{wiki_info['generation']}のVTuberだよ！ {wiki_info['description']}"]
                    if wiki_info.get('graduation_date'):
                        response_parts.append(f"でもね、{wiki_info['graduation_date']}に卒業しちゃったんだ…。{wiki_info.get('mochiko_feeling', 'まじ寂しいよね…。')}")
                    ai_text = " ".join(response_parts)
        
        # === 優先度2: さくらみこ特別応答 ===
        elif not ai_text and ('さくらみこ' in message or 'みこち' in message):
            special_responses = get_sakuramiko_special_responses()
            for keyword, response in special_responses.items():
                if keyword in message:
                    ai_text = response
                    break
        
        # === 優先度3: ニュース詳細 ===
        if not ai_text and (news_number := is_news_detail_request(message)):
            news_detail = get_cached_news_detail(session, user_uuid, news_number)
            if news_detail:
                ai_text = generate_ai_response(
                    user_data, 
                    f"「{news_detail.title}」についてだね！", 
                    history, 
                    f"ニュースの詳細情報:\n{news_detail.content}", 
                    is_detailed=True
                )
        
        # === 優先度4: 時間・天気 ===
        elif not ai_text and (is_time_request(message) or is_weather_request(message)):
            responses = []
            if is_time_request(message): 
                responses.append(get_japan_time())
            if is_weather_request(message): 
                responses.append(get_weather_forecast(extract_location(message)))
            ai_text = " ".join(responses)
        
        # === 優先度5: ホロライブニュース ===
        elif not ai_text and is_hololive_request(message) and any(kw in message for kw in ['ニュース', '最新', '情報', 'お知らせ']):
            all_news = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(10).all()
            if all_news:
                selected_news = random.sample(all_news, min(random.randint(3, 5), len(all_news)))
                save_news_cache(session, user_uuid, selected_news, 'hololive')
                
                news_items_text = []
                for i, n in enumerate(selected_news, 1):
                    short_title = n.title[:50] + "..." if len(n.title) > 50 else n.title
                    news_items_text.append(f"【{i}】{short_title}")

                news_text = f"ホロライブの最新ニュース、{len(selected_news)}件紹介するね！\n" + "\n".join(news_items_text) + "\n\n気になるのあった？番号で教えて！"
                ai_text = limit_text_for_sl(news_text, 250)
            else:
                ai_text = "ごめん、今ニュースがまだ取得できてないみたい…"
        
        # === 優先度6: 追加質問 ===
        elif not ai_text and is_follow_up:
            logger.info("🔍 Processing follow-up question")
            last_assistant_msg = None
            for h in history:
                if h.role == 'assistant':
                    last_assistant_msg = h.content
                    break
            
            if last_assistant_msg:
                ai_text = generate_ai_response(
                    user_data,
                    message,
                    history,
                    f"直前の回答内容:\n{last_assistant_msg}",
                    is_detailed=True
                )
            else:
                ai_text = generate_ai_response(user_data, message, history)
        
        # === 優先度7: 明示的な検索リクエスト ===
        elif not ai_text and is_explicit_search_request(message):
            if start_background_search(user_uuid, message, is_detailed_request(message)):
                ai_text = "おっけー、調べてみるね！ちょっと待ってて！"
            else:
                ai_text = "ごめん、今検索機能がうまく動いてないみたい…"

        # === 優先度8: 感情・季節・面白い話 ===
        elif not ai_text and (is_emotional_expression(message) or is_seasonal_topic(message) or is_story_request(message)):
             ai_text = generate_ai_response(user_data, message, history)
        
        # === 優先度9: 暗黙的な検索リクエスト ===
        elif not ai_text and not is_short_response(message) and should_search(message):
            if start_background_search(user_uuid, message, is_detailed_request(message)):
                ai_text = "おっけー、調べてみるね！結果が出るまでちょっと待ってて！"
            else:
                ai_text = "ごめん、今検索機能がうまく動いてないみたい…"
        
        # === 優先度10: 通常会話 ===
        elif not ai_text:
            ai_text = generate_ai_response(user_data, message, history)
        
        # ★ 最終的な文字数制限
        ai_text = limit_text_for_sl(ai_text, SL_SAFE_CHAR_LIMIT)
        
        # 会話履歴に保存
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()
        
        logger.info(f"✅ Responded: {ai_text[:80]}")
        return f"{ai_text}|", 200
        
    except Exception as e:
        logger.error(f"❌ Unhandled error in chat endpoint: {e}", exc_info=True)
        return "ごめん、システムエラーが起きちゃった…|", 500
    finally:
        if session:
            session.close()


# ========================================
# 【追加5】DB修正履歴を確認するエンドポイント
# ========================================

@app.route('/correction_history', methods=['GET'])
def get_correction_history():
    """DB修正履歴を取得（管理者用）"""
    session = Session()
    try:
        corrections = session.query(BackgroundTask).filter_by(
            task_type='db_correction'
        ).order_by(BackgroundTask.completed_at.desc()).limit(50).all()
        
        history = []
        for task in corrections:
            if task.query:
                try:
                    query_data = json.loads(task.query)
                    history.append({
                        'task_id': task.task_id,
                        'member_name': query_data.get('member_name'),
                        'correction_type': query_data.get('correction_type'),
                        'verified': query_data.get('verified'),
                        'corrected': query_data.get('corrected'),
                        'completed_at': task.completed_at.isoformat() if task.completed_at else None
                    })
                except:
                    pass
        
        return Response(
            json.dumps({'corrections': history}, ensure_ascii=False, indent=2),
            status=200,
            mimetype='application/json; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"❌ Correction history error: {e}")
        return Response(
            json.dumps({'error': str(e)}, ensure_ascii=False),
            status=500,
            mimetype='application/json; charset=utf-8'
        )
    finally:
        session.close()

@app.route('/test_voicevox', methods=['GET'])
def test_voicevox():
    """VOICEVOX接続テスト"""
    voicevox_url = VOICEVOX_URL_FROM_ENV or "http://localhost:50021"
    
    result = {
        'voicevox_url': voicevox_url,
        'voicevox_enabled': VOICEVOX_ENABLED,
        'voice_directory': {
            'path': VOICE_DIR,
            'exists': os.path.exists(VOICE_DIR),
            'writable': os.access(VOICE_DIR, os.W_OK) if os.path.exists(VOICE_DIR) else False
        },
        'tests': {}
    }
    
    # Test 1: VOICEVOXバージョン確認
    try:
        response = requests.get(f"{voicevox_url}/version", timeout=5)
        if response.ok:
            result['tests']['version'] = {
                'status': 'ok',
                'data': response.json()
            }
        else:
            result['tests']['version'] = {
                'status': 'error',
                'http_code': response.status_code
            }
    except Exception as e:
        result['tests']['version'] = {
            'status': 'error',
            'message': str(e)
        }
    
    # Test 2: 簡易音声生成テスト
    try:
        test_response = requests.post(
            f"{voicevox_url}/audio_query",
            params={"text": "テスト", "speaker": VOICEVOX_SPEAKER_ID},
            timeout=10
        )
        if test_response.ok:
            result['tests']['audio_query'] = {
                'status': 'ok',
                'message': '音声クエリ生成が正常に動作しています'
            }
        else:
            result['tests']['audio_query'] = {
                'status': 'error',
                'http_code': test_response.status_code
            }
    except Exception as e:
        result['tests']['audio_query'] = {
            'status': 'error',
            'message': str(e)
        }
    
    # 総合判定
    all_ok = all(
        test.get('status') == 'ok' 
        for test in result['tests'].values()
    ) and result['voice_directory']['exists'] and result['voice_directory']['writable']
    
    result['overall_status'] = 'ok' if all_ok else 'error'
    
    if not all_ok:
        result['recommendations'] = []
        if not result['voice_directory']['exists']:
            result['recommendations'].append('音声ディレクトリが存在しません - サーバーを再起動してください')
        if not result['voice_directory']['writable']:
            result['recommendations'].append('音声ディレクトリに書き込み権限がありません')
        if result['tests'].get('version', {}).get('status') != 'ok':
            result['recommendations'].append('VOICEVOXエンジンに接続できません')
    
    return jsonify(result), 200 if all_ok else 500
    
# --- Flaskエンドポイント ---
@app.route('/health', methods=['GET'])
def health_check():
    """ヘルスチェックエンドポイント - Renderの起動確認用"""
    try:
        # データベース接続確認
        with engine.connect() as conn: 
            conn.execute(text("SELECT 1"))
        db_status = 'ok'
    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        db_status = 'error'
    
    health_data = {
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'services': {
            'database': db_status, 
            'groq_ai': 'ok' if groq_client else 'disabled'
        }
    }
    
    logger.info(f"Health check: {health_data}")
    return jsonify(health_data), 200

# ========================================
# 【修正1】check_task エンドポイント - 文字化け対策
# ========================================

@app.route('/check_task', methods=['POST'])
def check_task():
    """バックグラウンドタスクの完了チェック（UTF-8文字化け対策版）"""
    try:
        data = request.json
        if not data:
            logger.error("❌ check_task: Empty request body")
            return Response(
                json.dumps({'status': 'error', 'message': 'リクエストボディが空です'}, ensure_ascii=False),
                status=400,
                mimetype='application/json; charset=utf-8'
            )
        
        user_uuid = data.get('uuid', '')
        
        if not user_uuid:
            logger.error("❌ check_task: UUID missing")
            return Response(
                json.dumps({'status': 'error', 'message': 'UUID required'}, ensure_ascii=False),
                status=400,
                mimetype='application/json; charset=utf-8'
            )
        
        logger.info(f"🔍 Checking tasks for user: {user_uuid}")
        
        # 完了タスクを確認
        completed_task = check_completed_tasks(user_uuid)
        
        if completed_task:
            logger.info(f"✅ Task completed for {user_uuid}: {completed_task['query']}")
            
            # AIによる報告メッセージ生成
            session = Session()
            try:
                user_data = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
                if not user_data:
                    user_name = "あなた"
                    user_uuid_for_response = user_uuid
                else:
                    user_name = user_data.user_name
                    user_uuid_for_response = user_data.user_uuid
                
                # 履歴を取得
                history = get_conversation_history(session, user_uuid)
                
                # 報告メッセージを生成
                report_message = generate_ai_response(
                    {'name': user_name, 'uuid': user_uuid_for_response},
                    f"（検索完了報告）以前リクエストされた「{completed_task['query']}」の結果を報告してください。",
                    history,
                    completed_task['result'],
                    is_detailed=True,
                    is_task_report=True
                )
                
                # ★ 修正: 文字数制限を適用
                report_message = limit_text_for_sl(report_message, SL_SAFE_CHAR_LIMIT)
                
                # 会話履歴に保存
                session.add(ConversationHistory(
                    user_uuid=user_uuid,
                    role='assistant',
                    content=report_message
                ))
                session.commit()
                
                # ★ 修正: ensure_ascii=False + UTF-8指定
                return Response(
                    json.dumps({
                        'status': 'completed',
                        'query': completed_task['query'],
                        'message': report_message
                    }, ensure_ascii=False, indent=2),
                    status=200,
                    mimetype='application/json; charset=utf-8'
                )
                
            except Exception as e:
                logger.error(f"❌ Report generation error: {e}", exc_info=True)
                session.rollback()
                return Response(
                    json.dumps({
                        'status': 'error',
                        'message': 'サーバーエラー'
                    }, ensure_ascii=False),
                    status=500,
                    mimetype='application/json; charset=utf-8'
                )
            finally:
                session.close()
        
        # タスクがまだ完了していない場合
        logger.info(f"⏳ Task pending for {user_uuid}")
        return Response(
            json.dumps({'status': 'pending'}, ensure_ascii=False),
            status=200,
            mimetype='application/json; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"❌ check_task critical error: {e}", exc_info=True)
        return Response(
            json.dumps({'status': 'error', 'message': 'サーバーエラー'}, ensure_ascii=False),
            status=500,
            mimetype='application/json; charset=utf-8'
        )

# ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼【ここからが変更箇所です】▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
# ========================================
# 【追加4】chat_lsl エンドポイントにDB修正検出を追加
# ========================================

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    session = Session()
    try:
        data = request.json
        user_uuid, user_name, message = data.get('uuid', ''), data.get('name', ''), data.get('message', '')
        
        if not all([user_uuid, user_name, message]):
            return "エラー: 必要な情報が足りないみたい…|", 400
        
        logger.info(f"💬 Received: {message} (from: {user_name})")
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        ai_text = ""
        
        # ★ 追加: DB修正リクエストの検出（最優先）
        correction_request = detect_db_correction_request(message)
        
        if correction_request:
            logger.info(f"🔧 DB correction request detected: {correction_request}")
            
            # バックグラウンドで検証 + 修正を開始
            if start_background_correction(user_uuid, correction_request):
                ai_text = f"え、まじで！？{correction_request['member_name']}ちゃんの情報、今すぐ調べて確認してみるね！ちょっと待ってて！"
            else:
                ai_text = "ごめん、今DB修正機能がうまく動いてないみたい…"
            
            # 会話履歴に保存
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
            session.commit()
            
            return f"{ai_text}|", 200
        
        # 以下、既存の処理（変更なし）
        
        # ★ 追加: 追加質問の判定
        is_follow_up = is_follow_up_question(message, history)
        
        # === 優先度1: ホロメン基本情報 ===
        holomem_keywords = get_active_holomem_keywords()
        basic_question_pattern = f"({'|'.join(re.escape(k) for k in holomem_keywords)})って(?:誰|だれ|何|なに)[\?？]?$"
        basic_question_match = re.search(basic_question_pattern, message.strip())
        
        if not ai_text and basic_question_match:
            member_name = basic_question_match.group(1)
            
            if member_name in ['ホロライブ', 'hololive', 'ホロメン']:
                ai_text = "ホロライブは、カバー株式会社が運営してるVTuber事務所のことだよ！ときのそらちゃんとか、たくさんの人気VTuberが所属してて、配信とかまじで楽しいからおすすめ！"
            else:
                wiki_info = get_holomem_info(member_name)
                if wiki_info:
                    response_parts = [f"{wiki_info['name']}ちゃんはね、ホロライブ{wiki_info['generation']}のVTuberだよ！ {wiki_info['description']}"]
                    if wiki_info.get('graduation_date'):
                        response_parts.append(f"でもね、{wiki_info['graduation_date']}に卒業しちゃったんだ…。{wiki_info.get('mochiko_feeling', 'まじ寂しいよね…。')}")
                    ai_text = " ".join(response_parts)
        
        # === 優先度2: さくらみこ特別応答 ===
        elif not ai_text and ('さくらみこ' in message or 'みこち' in message):
            special_responses = get_sakuramiko_special_responses()
            for keyword, response in special_responses.items():
                if keyword in message:
                    ai_text = response
                    break
        
        # === 優先度3: ニュース詳細 ===
        if not ai_text and (news_number := is_news_detail_request(message)):
            news_detail = get_cached_news_detail(session, user_uuid, news_number)
            if news_detail:
                ai_text = generate_ai_response(
                    user_data, 
                    f"「{news_detail.title}」についてだね！", 
                    history, 
                    f"ニュースの詳細情報:\n{news_detail.content}", 
                    is_detailed=True
                )
        
        # === 優先度4: 時間・天気 ===
        elif not ai_text and (is_time_request(message) or is_weather_request(message)):
            responses = []
            if is_time_request(message): 
                responses.append(get_japan_time())
            if is_weather_request(message): 
                responses.append(get_weather_forecast(extract_location(message)))
            ai_text = " ".join(responses)
        
        # === 優先度5: ホロライブニュース ===
        elif not ai_text and is_hololive_request(message) and any(kw in message for kw in ['ニュース', '最新', '情報', 'お知らせ']):
            all_news = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(10).all()
            if all_news:
                selected_news = random.sample(all_news, min(random.randint(3, 5), len(all_news)))
                save_news_cache(session, user_uuid, selected_news, 'hololive')
                
                news_items_text = []
                for i, n in enumerate(selected_news, 1):
                    short_title = n.title[:50] + "..." if len(n.title) > 50 else n.title
                    news_items_text.append(f"【{i}】{short_title}")

                news_text = f"ホロライブの最新ニュース、{len(selected_news)}件紹介するね！\n" + "\n".join(news_items_text) + "\n\n気になるのあった？番号で教えて！"
                ai_text = limit_text_for_sl(news_text, 250)
            else:
                ai_text = "ごめん、今ニュースがまだ取得できてないみたい…"
        
        # === 優先度6: 追加質問 ===
        elif not ai_text and is_follow_up:
            logger.info("🔍 Processing follow-up question")
            last_assistant_msg = None
            for h in history:
                if h.role == 'assistant':
                    last_assistant_msg = h.content
                    break
            
            if last_assistant_msg:
                ai_text = generate_ai_response(
                    user_data,
                    message,
                    history,
                    f"直前の回答内容:\n{last_assistant_msg}",
                    is_detailed=True
                )
            else:
                ai_text = generate_ai_response(user_data, message, history)
        
        # === 優先度7: 明示的な検索リクエスト ===
        elif not ai_text and is_explicit_search_request(message):
            if start_background_search(user_uuid, message, is_detailed_request(message)):
                ai_text = "おっけー、調べてみるね！ちょっと待ってて！"
            else:
                ai_text = "ごめん、今検索機能がうまく動いてないみたい…"

        # === 優先度8: 感情・季節・面白い話 ===
        elif not ai_text and (is_emotional_expression(message) or is_seasonal_topic(message) or is_story_request(message)):
             ai_text = generate_ai_response(user_data, message, history)
        
        # === 優先度9: 暗黙的な検索リクエスト ===
        elif not ai_text and not is_short_response(message) and should_search(message):
            if start_background_search(user_uuid, message, is_detailed_request(message)):
                ai_text = "おっけー、調べてみるね！結果が出るまでちょっと待ってて！"
            else:
                ai_text = "ごめん、今検索機能がうまく動いてないみたい…"
        
        # === 優先度10: 通常会話 ===
        elif not ai_text:
            ai_text = generate_ai_response(user_data, message, history)
        
        # ★ 最終的な文字数制限
        ai_text = limit_text_for_sl(ai_text, SL_SAFE_CHAR_LIMIT)
        
        # 会話履歴に保存
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()
        
        logger.info(f"✅ Responded: {ai_text[:80]}")
        return f"{ai_text}|", 200
        
    except Exception as e:
        logger.error(f"❌ Unhandled error in chat endpoint: {e}", exc_info=True)
        return "ごめん、システムエラーが起きちゃった…|", 500
    finally:
        if session:
            session.close()

# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲【ここまでが変更箇所です】▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲
# ===== 【追加】心理分析コマンド =====

@app.route('/analyze_psychology', methods=['POST'])
def analyze_psychology_endpoint():
    """心理分析を実行するエンドポイント"""
    try:
        data = request.json
        user_uuid = data.get('uuid')
        
        if not user_uuid:
            return jsonify({'error': 'UUID required'}), 400
        
        # バックグラウンドで分析実行
        background_executor.submit(analyze_user_psychology, user_uuid)
        
        return jsonify({
            'status': 'started',
            'message': '心理分析を開始しました。完了まで少しお待ちください。'
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Psychology analysis endpoint error: {e}")
        return jsonify({'error': str(e)}), 500


# ========================================
# 【修正5】get_psychology エンドポイント - 文字化け対策
# ========================================

@app.route('/get_psychology', methods=['POST'])
def get_psychology_endpoint():
    """心理分析結果を取得（UTF-8対応版）"""
    try:
        data = request.json
        user_uuid = data.get('uuid')
        
        if not user_uuid:
            return Response(
                json.dumps({'error': 'UUID required'}, ensure_ascii=False),
                status=400,
                mimetype='application/json; charset=utf-8'
            )
        
        psychology = get_user_psychology(user_uuid)
        
        if not psychology:
            return Response(
                json.dumps({'error': 'No analysis data found'}, ensure_ascii=False),
                status=404,
                mimetype='application/json; charset=utf-8'
            )
        
        return Response(
            json.dumps(psychology, ensure_ascii=False, indent=2),
            status=200,
            mimetype='application/json; charset=utf-8'
        )
        
    except Exception as e:
        logger.error(f"❌ Get psychology error: {e}")
        return Response(
            json.dumps({'error': str(e)}, ensure_ascii=False),
            status=500,
            mimetype='application/json; charset=utf-8'
        )


# ===== 【追加】定期的な心理分析スケジュール =====
# initialize_app() 関数内のスケジューラー設定に追加

def schedule_psychology_analysis():
    """全ユーザーの心理分析を定期実行"""
    session = Session()
    try:
        # 最近活動したユーザーを取得
        active_users = session.query(UserMemory).filter(
            UserMemory.last_interaction > datetime.utcnow() - timedelta(days=7),
            UserMemory.interaction_count >= 10
        ).all()
        
        for user in active_users:
            # 最後の分析から24時間以上経過しているユーザーのみ
            psychology = session.query(UserPsychology).filter_by(user_uuid=user.user_uuid).first()
            
            if not psychology or psychology.last_analyzed < datetime.utcnow() - timedelta(hours=24):
                logger.info(f"🧠 Scheduling psychology analysis for user: {user.user_name}")
                background_executor.submit(analyze_user_psychology, user.user_uuid)
                time.sleep(5)  # 負荷分散のため5秒待機
        
    except Exception as e:
        logger.error(f"❌ Schedule psychology analysis error: {e}")
    finally:
        session.close()
        
@app.route('/generate_voice', methods=['POST'])
def voice_generation_endpoint():
    """音声生成エンドポイント - 修正版"""
    try:
        # request.jsonがNoneの場合の対策
        data = request.json
        if not data:
            logger.error("❌ Empty request body")
            return jsonify({'error': 'リクエストボディが空です'}), 400
        
        # テキスト取得（先に切り詰めない）
        text = data.get('text', '').strip()
        
        if not text:
            logger.error("❌ No text provided")
            return jsonify({'error': 'テキストが指定されていません'}), 400
        
        # ★ 正しい文字数制限（修正版）
        original_length = len(text)
        if original_length > 200:
            text = limit_text_for_sl(text, 150)
            logger.warning(f"⚠️ Text truncated: {original_length} → {len(text)} chars")
        
        logger.info(f"🎤 Voice generation: {text[:50]}...")
        
        # 音声生成
        voice_path = generate_voice(text)
        
        # 結果確認
        if not voice_path:
            logger.error("❌ generate_voice() returned None")
            return jsonify({
                'error': '音声生成に失敗しました',
                'details': 'VOICEVOXエンジンに接続できません'
            }), 500
        
        if not os.path.exists(voice_path):
            logger.error(f"❌ Voice file not found: {voice_path}")
            return jsonify({
                'error': '音声ファイルが見つかりません'
            }), 500
        
        # 成功レスポンス
        filename = os.path.basename(voice_path)
        voice_url = f"{SERVER_URL}/voices/{filename}"
        
        logger.info(f"✅ Voice generated: {filename}")
        
        return jsonify({
            'status': 'success',
            'filename': filename,
            'url': voice_url,
            'text': text
        }), 200
        
    except AttributeError as e:
        logger.error(f"❌ AttributeError (request.json is None?): {e}")
        return jsonify({
            'error': 'リクエスト形式が不正です',
            'details': 'Content-Type: application/json が必要です'
        }), 400
        
    except Exception as e:
        logger.error(f"❌ Voice generation exception: {e}", exc_info=True)
        return jsonify({
            'error': '音声生成中にエラーが発生しました',
            'details': str(e)
        }), 500

@app.route('/voices/<filename>')
def serve_voice_file(filename):
    return send_from_directory(VOICE_DIR, filename)

# ↓↓↓ ここに追加 ↓↓↓
@app.route('/play_voice')
def play_voice():
    """音声ファイルを自動再生するHTMLページ"""
    voice_url = request.args.get('url', '')
    
    if not voice_url:
        return "音声URLが指定されていません", 400
    
    if not voice_url.startswith(SERVER_URL):
        return "不正な音声URLです", 400
    
    return f'''<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>もちこAI 音声再生</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            width: 100%;
            height: 100%;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex;
            justify-content: center;
            align-items: center;
            font-family: 'Segoe UI', Arial, sans-serif;
            overflow: hidden;
        }}
        .player {{
            background: rgba(255, 255, 255, 0.95);
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3);
            text-align: center;
            max-width: 400px;
        }}
        .emoji {{
            font-size: 3em;
            animation: pulse 1.5s infinite;
        }}
        @keyframes pulse {{
            0%, 100% {{ transform: scale(1); }}
            50% {{ transform: scale(1.1); }}
        }}
        h1 {{
            color: #667eea;
            margin: 15px 0 10px 0;
            font-size: 1.8em;
        }}
        p {{ color: #666; margin-bottom: 20px; }}
        audio {{ width: 100%; margin-top: 10px; }}
        .status {{
            margin-top: 15px;
            padding: 10px;
            background: #e8f5e9;
            border-radius: 5px;
            color: #2e7d32;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <div class="player">
        <div class="emoji">🎤</div>
        <h1>もちこAI</h1>
        <p>音声を再生しています...</p>
        <audio id="audioPlayer" controls autoplay>
            <source src="{voice_url}" type="audio/wav">
        </audio>
        <div class="status" id="status">準備中...</div>
    </div>
    <script>
        const audio = document.getElementById('audioPlayer');
        const status = document.getElementById('status');
        audio.addEventListener('loadstart', () => {{
            status.textContent = '音声を読み込んでいます...';
            status.style.background = '#fff3e0';
            status.style.color = '#e65100';
        }});
        audio.addEventListener('play', () => {{
            status.textContent = '♪ 再生中...';
            status.style.background = '#e3f2fd';
            status.style.color = '#1565c0';
        }});
        audio.addEventListener('ended', () => {{
            status.textContent = '✓ 再生完了';
        }});
        audio.addEventListener('error', () => {{
            status.textContent = '✗ 読み込み失敗';
            status.style.background = '#ffebee';
            status.style.color = '#c62828';
        }});
        audio.play().catch(() => {{
            status.textContent = '▶ 再生ボタンを押してください';
            status.style.background = '#fff3e0';
            status.style.color = '#e65100';
        }});
    </script>
</body>
</html>'''

@app.route('/play_voice_simple')
def play_voice_simple():
    """最小限のHTMLで音声再生"""
    voice_url = request.args.get('url', '')
    if not voice_url:
        return "音声URLが指定されていません", 400
    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body{{margin:0;background:#667eea;display:flex;justify-content:center;align-items:center;height:100vh;}}
audio{{width:90%;max-width:400px;}}
</style></head><body>
<audio controls autoplay><source src="{voice_url}" type="audio/wav"></audio>
</body></html>'''

@app.route('/stats', methods=['GET'])
def get_stats():
    session = Session()
    try:
        stats = {
            'users': session.query(UserMemory).count(),
            'conversations': session.query(ConversationHistory).count(),
            'hololive_news': session.query(HololiveNews).count(),
            'specialized_news': session.query(SpecializedNews).count(),
            'holomem_wiki_entries': session.query(HolomemWiki).count(),
        }
        return jsonify(stats)
    finally:
        session.close()

# --- アプリケーション初期化 ---

# 【修正版】initialize_app 関数
def initialize_app():
    global engine, Session, groq_client
    logger.info("=" * 60)
    logger.info("🔧 Starting Mochiko AI initialization...")
    logger.info("=" * 60)

    try:
        logger.info("🚀 Step 1/6: Initializing Gemini...")
        initialize_gemini_client()
    except Exception as e:
        logger.warning(f"⚠️ Gemini initialization failed: {e}")

    try:
        logger.info("📡 Step 2/6: Initializing Groq client...")
        groq_client = initialize_groq_client()
        if groq_client:
            logger.info("✅ Groq client ready")
        else:
            logger.warning("⚠️ Groq client disabled - using fallback responses")
    except Exception as e:
        logger.warning(f"⚠️ Groq initialization failed but continuing: {e}")
        groq_client = None

    try:
        logger.info("🗄️ Step 3/6: Initializing database...")
        engine = create_optimized_db_engine()
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        logger.info("✅ Database initialized successfully")
    except Exception as e:
        logger.critical(f"🔥 Database initialization failed: {e}")
        raise

    # ニュースデータとメンバーデータのチェック
    session = Session()
    try:
        logger.info("📰 Step 4/6: Checking news + member data...")
        holo_count = session.query(HololiveNews).count()
        member_count = session.query(HolomemWiki).count()
        
        if holo_count == 0 or member_count == 0:
            logger.info("🚀 First run: Scheduling Hololive news + members fetch...")
            background_executor.submit(update_hololive_news_database)
        else:
            logger.info(f"✅ Found {holo_count} Hololive news, {member_count} members")
            latest_member = session.query(HolomemWiki).order_by(HolomemWiki.last_updated.desc()).first()
            if latest_member and latest_member.last_updated < datetime.utcnow() - timedelta(hours=24):
                logger.info("⏰ Member data is stale, scheduling update...")
                background_executor.submit(update_hololive_news_database)
        
        spec_count = session.query(SpecializedNews).count()
        if spec_count == 0:
            logger.info("🚀 First run: Scheduling specialized news fetch...")
            background_executor.submit(update_all_specialized_news)
        else:
            logger.info(f"✅ Found {spec_count} specialized news items")
            
    except Exception as e:
        logger.warning(f"⚠️ News initialization check failed but continuing: {e}")
    finally:
        session.close()

    try:
        logger.info("⏰ Step 5/6: Starting scheduler...")
        schedule.every().hour.do(update_hololive_news_database)
        schedule.every(3).hours.do(update_all_specialized_news)
        schedule.every().day.at("02:00").do(cleanup_old_data_advanced)
        
        def run_scheduler():
            while True:
                try:
                    schedule.run_pending()
                except Exception as e:
                    logger.error(f"❌ Scheduler error: {e}")
                time.sleep(60)

        scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
        scheduler_thread.start()
        logger.info("✅ Scheduler started successfully")
    except Exception as e:
        logger.error(f"❌ Scheduler initialization failed: {e}")
    
    logger.info("=" * 60)
    logger.info("✅ Mochiko AI initialization complete!")
    logger.info("🌐 Server is ready to accept requests")
    logger.info("=" * 60)
    
def signal_handler(sig, frame):
    logger.info(f"🛑 Signal {sig} received. Shutting down gracefully...")
    background_executor.shutdown(wait=True)
    if 'engine' in globals() and engine:
        engine.dispose()
    logger.info("👋 Mochiko AI has shut down.")
    sys.exit(0)

# ========================================
# 【変更7】HOLOMEM_KEYWORDSをDB自動生成に変更
# ========================================

def get_active_holomem_keywords():
    """現役メンバーの名前リストをDBから取得"""
    session = Session()
    try:
        members = session.query(HolomemWiki.member_name).filter_by(is_active=True).all()
        keywords = [m[0] for m in members] + ['ホロライブ', 'ホロメン', 'hololive', 'YAGOO']
        return keywords
    except Exception as e:
        logger.error(f"❌ Failed to get holomem keywords: {e}")
        # フォールバック
        return ['ホロライブ', 'ホロメン', 'hololive']
    finally:
        session.close()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ========================================
# 【変更8】is_hololive_request を動的キーワード取得に変更
# ========================================

def is_hololive_request(message):
    """ホロライブ関連の質問かどうか判定（動的キーワード）"""
    keywords = get_active_holomem_keywords()
    return any(keyword in message for keyword in keywords)


# ========================================
# 【変更9】chat_lsl エンドポイントの基本情報判定を動的化
# ========================================

# chat_lsl 関数内の該当箇所を以下に置き換え:


# ========================================
# 【変更10】background_deep_search のホロメン判定を動的化
# ========================================

# background_deep_search 関数内の該当箇所を以下に置き換え:

# Step 3: ホロメン検索（既存のロジック）
holomem_keywords = get_active_holomem_keywords()
holomem_matched = None
query_topic = ""

for member_name in holomem_keywords:
    if member_name in query:
        holomem_matched = member_name
        query_topic = query.replace(member_name, '').replace('について', '').replace('教えて', '').strip()
        if not query_topic:
            query_topic = "概要"
        break

if holomem_matched:
    wiki_info = get_holomem_info(holomem_matched)
    if wiki_info and query_topic == "概要":
        search_result = f"{holomem_matched}に関するデータベース情報:\n{wiki_info['description']}"
    else:
        search_result = deep_web_search(f"ホロライブ {holomem_matched} {query_topic}", is_detailed)

# --- メイン実行 ---
try:
    initialize_app()
    application = app
    logger.info("✅ Flask application 'application' is ready and initialized.")
except Exception as e:
    logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
    # エラーが発生してもアプリケーションオブジェクトは作成する
    application = app
    logger.warning("⚠️ Application created with limited functionality due to initialization error.")

if __name__ == '__main__':
    logger.info("🚀 Running in direct mode (not recommended for production)")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
