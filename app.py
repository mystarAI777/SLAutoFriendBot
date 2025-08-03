// ================================================================ //
// SL Auto Friend Bot - LSL Client Script (v5.0 - 連続対話モード版) //
// ================================================================ //
// ユーザー体験を向上させるため、対話方式を大幅に改善したバージョン //
// ・一度のタッチで「連続対話モード」を開始します。                 //
// ・対話モード中は、再度タッチするまでチャットに応答し続けます。   //
// ・あなたとの会話中に、他の人がタッチしても邪魔されません。       //
// ================================================================ //


// --- ▼▼▼ 設定項目 ▼▼▼ ---
//【注意！】末尾が「/chat_lsl」になっていることを確認してください。
string SERVER_URL = "https://slautofriendbot.onrender.com/chat_lsl"; 
// 会話が途切れた後、自動でリセットされるまでの時間（秒）
float INACTIVITY_TIMEOUT = 90.0; 
// --- ▲▲▲ 設定項目はここまで ▲▲▲ ---


// --- グローバル変数 ---
key     gUserKey;       // 現在会話中のユーザーキー
key     gHttpRequestId;
integer gListenHandle;

// --- 状態管理のための定数 ---
integer STATE_IDLE = 0;                     // 0: 誰とも話していない待機状態
integer STATE_LISTENING = 1;                // 1: ユーザーの発言を待っている状態
integer STATE_WAITING_FOR_RESPONSE = 2;     // 2: サーバーからの返事を待っている状態
integer gCurrentState;                      // 現在の状態を保持する変数

vector  TEXT_COLOR = <1.0, 1.0, 1.0>;
float   TEXT_ALPHA = 1.0;


// --- 状態を完全にリセットする関数 ---
reset() {
    llSetTimerEvent(0.0);
    if (gListenHandle != 0) {
        llListenRemove(gListenHandle);
        gListenHandle = 0;
    }
    llSetText("", <0,0,0>, 0.0);
    gUserKey = NULL_KEY;
    gCurrentState = STATE_IDLE;
    llSetObjectDesc("待機中... 私にタッチして会話を始めてね。");
}


// --- メインのステートブロック ---
default
{
    state_entry() {
        llSay(0, "AIボット「もちこ」が起動しました。(v5.0 - 連続対話モード)");
        if (llSubStringIndex(SERVER_URL, "/chat_lsl") == -1) {
            llOwnerSay("【設定エラー】SERVER_URLの末尾が「/chat_lsl」になっていません。");
        }
        reset();
    }

    touch_start(integer total_number) {
        key toucher = llDetectedKey(0);

        // 誰も使っていない時に、誰かがタッチした場合
        if (gCurrentState == STATE_IDLE) {
            gUserKey = toucher;
            string name = llDetectedName(0);
            llInstantMessage(gUserKey, "こんにちは！連続対話モードを開始します。\n近くのチャットで話しかけてください。\n会話を終わる時は、私をもう一度タッチしてくださいね。");
            gListenHandle = llListen(0, "", gUserKey, "");
            gCurrentState = STATE_LISTENING;
            llSetObjectDesc(name + "さんと連続会話中...");
            llSetTimerEvent(INACTIVITY_TIMEOUT); // 無操作タイムアウトを開始
        }
        // 会話中に、会話を始めた本人がタッチした場合
        else if (toucher == gUserKey) {
            llInstantMessage(gUserKey, "会話を終了します。またね！");
            reset();
        }
        // 会話中に、別人がタッチした場合
        else {
            string current_user_name = llKey2Name(gUserKey);
            llInstantMessage(toucher, "ごめんなさい、今は " + current_user_name + " さんとお話中です。");
        }
    }

    listen(integer channel, string name, key id, string message) {
        // 発言者が本人で、かつ発言を待っている状態でなければ無視
        if (id != gUserKey || gCurrentState != STATE_LISTENING) return;

        gCurrentState = STATE_WAITING_FOR_RESPONSE; // サーバーの返答待ち状態に移行
        llSetText("考え中...", TEXT_COLOR, TEXT_ALPHA);
        llSetTimerEvent(0.0); // 応答待ちの間はタイムアウトを一時停止

        // JSON文字列を手動で構築
        string escaped_name = llDumpList2String(llParseString2List(name, ["\""], []), "\\\"");
        string escaped_message = llDumpList2String(llParseString2List(message, ["\""], []), "\\\"");
        string json_body = "{" +
            "\"uuid\":\"" + (string)id + "\"," +
            "\"name\":\"" + escaped_name + "\"," +
            "\"message\":\"" + escaped_message + "\"" +
        "}";

        gHttpRequestId = llHTTPRequest(SERVER_URL, [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"], json_body);
    }

    http_response(key request_id, integer status, list metadata, string body) {
        if (request_id != gHttpRequestId) return;
        llSetText("", <0,0,0>, 0.0);

        if (status != 200) {
            llSay(0, "うぅ、サーバーと通信できなかったみたい…ごめんなさい。");
            llInstantMessage(gUserKey, "エラーが発生したので、会話を終了します。");
            reset();
            return;
        }

        // パイプ区切りのテキストを処理
        list parts = llParseString2List(body, ["|"], []);
        string ai_text = llList2String(parts, 0);
        string audio_url = "";
        if (llGetListLength(parts) > 1) {
            audio_url = llList2String(parts, 1);
        }
        
        if (ai_text != "") {
            llSay(0, ai_text);
        } else {
            llSay(0, "あれ？何か言うのを忘れちゃったみたい。");
        }
        
        // 音声再生
        if (audio_url != "" && llGetSubString(audio_url, 0, 0) == "/") {
            string base_url = llGetSubString(SERVER_URL, 0, llSubStringIndex(SERVER_URL, "/chat_lsl") - 1);
            llPlaySound(base_url + audio_url, 1.0);
        }

        // 再びリスニング状態に戻る
        gCurrentState = STATE_LISTENING;
        llSetTimerEvent(INACTIVITY_TIMEOUT); // 再び無操作タイムアウトを開始
    }
    
    timer() {
        // 無操作タイムアウトが発生した場合
        llInstantMessage(gUserKey, "しばらくお返事がないので、会話を終了しました。また話したくなったらタッチしてね！");
        reset();
    }
    
    on_rez(integer start_param) {
        llResetScript();
    }
}
