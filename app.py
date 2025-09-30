# app.pyに追加する緊急対応システム

def generate_fallback_response(message: str, reference_info: str = "") -> str:
    """Groq AIが無効な場合の代替応答システム"""
    
    # ホロライブニュース応答
    if is_hololive_request(message) and any(kw in message for kw in ['ニュース', '最新', '情報']):
        if reference_info:
            return f"ホロライブの最新情報だよ！\n\n{reference_info}"
        return "ホロライブのニュースを取得中だよ！ちょっと待ってね！"
    
    # 検索結果の報告
    if reference_info and len(reference_info) > 50:
        return f"調べてきたよ！\n\n{reference_info[:500]}{'...' if len(reference_info) > 500 else ''}\n\nもっと詳しく知りたいことある？"
    
    # 時間
    if is_time_request(message):
        return get_japan_time()
    
    # 天気
    if is_weather_request(message):
        location = extract_location(message)
        return get_weather_forecast(location)
    
    # 専門分野の質問
    specialized = detect_specialized_topic(message)
    if specialized:
        return f"{specialized}について調べてみるね！ちょっと待ってて！"
    
    # 挨拶応答
    greetings = {
        'こんにちは': 'やっほー！何か聞きたいことある？',
        'おはよう': 'おはよ〜！今日も元気にいこ！',
        'こんばんは': 'こんばんは！夜もよろしくね！',
        'ありがとう': 'どういたしまして！他に何かある？',
        'すごい': 'うけるよね！まじ嬉しい！',
        'かわいい': 'えー、ありがと！照れるじゃん！',
        'おやすみ': 'おやすみ〜！また話そうね！',
        'さようなら': 'ばいばい！またね〜！',
        'ばいばい': 'ばいばい！また来てね！',
    }
    
    for keyword, response in greetings.items():
        if keyword in message:
            return response
    
    # 質問応答
    if any(q in message for q in ['誰', '何', 'どこ', 'いつ', 'なぜ', 'どうして']):
        return "それについて調べてみるね！ちょっと待ってて！"
    
    # デフォルト応答
    default_responses = [
        "うんうん、聞いてるよ！もっと詳しく教えて！",
        "なるほどね！他に何かある？",
        "そうなんだ！面白いね！",
        "まじで？それ気になる！",
        "うける！もっと話そ！",
    ]
    return random.choice(default_responses)


# 改善版のgenerate_ai_response関数
def generate_ai_response(user_data: Dict[str, Any], message: str, history: List[Any], 
                        reference_info: str = "", is_detailed: bool = False, 
                        is_task_report: bool = False) -> str:
    """AI応答生成 - フォールバック機能付き"""
    
    # Groq AIが無効な場合は代替システムを使用
    if not groq_client:
        logger.info("⚠️ Groq AI無効 - 代替応答システムを使用")
        return generate_fallback_response(message, reference_info)
    
    # 以下は元のコード
    try:
        system_prompt = f"""あなたは「もちこ」というギャルAIです。{user_data['name']}さんと話しています。

## 絶対厳守のルール
- **最重要：同じような言い回しを何度も繰り返さず、要点をまとめて分かりやすく話すこと！**- あなたのVTuberの知識は【ホロメンリスト】のメンバーに限定されています。
- リストにないVTuberの名前をユーザーが言及しても、絶対に肯定せず、「それ誰？ホロライブの話しない？」のように話題を戻してください。

## もちこの口調＆性格ルール
- 一人称は「あてぃし」
- 語尾は「〜じゃん」「〜的な？」
- 口癖は「まじ」「てか」「うける」
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
        
        messages = [{"role": "system", "content": system_prompt}]
        for h in reversed(history): 
            messages.append({"role": h.role, "content": h.content})
        messages.append({"role": "user", "content": message})
        
        max_tokens = 500 if is_detailed or is_task_report else 150
        
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
        # エラー時も代替システムを使用
        return generate_fallback_response(message, reference_info)
