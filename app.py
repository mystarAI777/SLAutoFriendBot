// ======================================================================= //
// SL Auto Friend Bot - LSL Client Script (v5.1 - 音声デバッグ強化版)    //
// ======================================================================= //
// 音が鳴らない問題の原因を特定するため、サーバーからの応答や生成したURLを //
// オーナーに報告するデバッグ機能を追加したバージョンです。                //
// ======================================================================= //

# ======================================================================= #
import os

// --- ▼▼▼ 設定項目 ▼▼▼ ---
string SERVER_URL = "https://slautofriendbot.onrender.com/chat_lsl"; 
float INACTIVITY_TIMEOUT = 90.0;
// --- ▲▲▲ 設定項目はここまで ▲▲▲ ---


// --- グローバル変数 ---
key     gUserKey;
key     gHttpRequestId;
integer gListenHandle;
integer gCurrentState;
integer STATE_IDLE = 0;
integer STATE_LISTENING = 1;
integer STATE_WAITING_FOR_RESPONSE = 2;
vector  TEXT_COLOR = <1.0, 1.0, 1.0>;
float   TEXT_ALPHA = 1.0;


// --- 自作関数 ---
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
        llSay(0, "AIボット「もちこ」が起動しました。(v5.1 - 音声デバッグモード)");
        reset();
    }

    touch_start(integer total_number) {
        key toucher = llDetectedKey(0);
        if (gCurrentState == STATE_IDLE) {
            gUserKey = toucher;
            string name = llDetectedName(0);
            llInstantMessage(gUserKey, "こんにちは！連続対話モードを開始します。\n会話を終わる時は、私をもう一度タッチしてください。");
            gListenHandle = llListen(0, "", gUserKey, "");
            gCurrentState = STATE_LISTENING;
            llSetObjectDesc(name + "さんと連続会話中...");
            llSetTimerEvent(INACTIVITY_TIMEOUT);
        }
        else if (toucher == gUserKey) {
            llInstantMessage(gUserKey, "会話を終了します。またね！");
            reset();
        }
        else {
            llInstantMessage(toucher, "ごめんなさい、今別の人とお話中です。");
        }
    }

    listen(integer channel, string name, key id, string message) {
        if (id != gUserKey || gCurrentState != STATE_LISTENING) return;
        gCurrentState = STATE_WAITING_FOR_RESPONSE;
        llSetText("考え中...", TEXT_COLOR, TEXT_ALPHA);
        llSetTimerEvent(0.0);
        string escaped_name = llDumpList2String(llParseString2List(name, ["\""], []), "\\\"");
        string escaped_message = llDumpList2String(llParseString2List(message, ["\""], []), "\\\"");
        string json_body = "{\"uuid\":\"" + (string)id + "\",\"name\":\"" + escaped_name + "\",\"message\":\"" + escaped_message + "\"}";
        gHttpRequestId = llHTTPRequest(SERVER_URL, [HTTP_METHOD, "POST", HTTP_MIMETYPE, "application/json"], json_body);
    }

    http_response(key request_id, integer status, list metadata, string body) {
        if (request_id != gHttpRequestId) return;
        llSetText("", <0,0,0>, 0.0);
        
        // ▼▼▼【ここからがデバッグ機能です】▼▼▼
        llOwnerSay("--------------------");
        llOwnerSay("サーバーからの応答(生データ): " + body);
        // ▲▲▲【デバッグ機能はここまで】▲▲▲

        if (status != 200) {
            llSay(0, "うぅ、サーバーと通信できなかったみたい…ごめんなさい。");
            reset();
            return;
        }

        list parts = llParseString2List(body, ["|"], []);
        string ai_text = llList2String(parts, 0);
        string audio_url_path = "";
        if (llGetListLength(parts) > 1) {
            audio_url_path = llList2String(parts, 1);
        }
        
        llOwnerSay("解析されたAIテキスト: " + ai_text);
        llOwnerSay("解析された音声パス: " + audio_url_path);

        if (ai_text != "") {
            llSay(0, ai_text);
        }
        
        if (audio_url_path != "" && llGetSubString(audio_url_path, 0, 0) == "/") {
            string base_url = llGetSubString(SERVER_URL, 0, llSubStringIndex(SERVER_URL, "/chat_lsl") - 1);
            string full_audio_url = base_url + audio_url_path;
            
            // ▼▼▼【ここからがデバッグ機能です】▼▼▼
            llOwnerSay("再生しようとしている最終URL: " + full_audio_url);
            // ▲▲▲【デバッグ機能はここまで】▲▲▲
            
            llPlaySound(full_audio_url, 1.0);
        } else {
            llOwnerSay("音声パスが無効か、空のため再生をスキップしました。");
        }

        gCurrentState = STATE_LISTENING;
        llSetTimerEvent(INACTIVITY_TIMEOUT);
    }
    
    timer() {
        llInstantMessage(gUserKey, "しばらくお返事がないので、会話を終了しました。");
        reset();
    }
    
    on_rez(integer start_param) {
        llResetScript();
    }
}
