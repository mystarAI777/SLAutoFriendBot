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
app.config['JSON_AS_ASCII'] = False

@app.after_request
def after_request(response):
    """CORSヘッダーを全レスポンスに追加"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

Base = declarative_base()

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

# --- アニメ検索機能 ---
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


# --- 心理分析機能 ---
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
            psychology = session.query(UserPsychology).filter_
