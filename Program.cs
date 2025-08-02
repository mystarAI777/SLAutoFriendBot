using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using SLVoicevoxServer.Data;
using Voicevox;
using System.Text.Json;

// --- 初期設定 ---
var builder = WebApplication.CreateBuilder(args);

var secretFilePath = "/etc/secrets/connection_string"; // Renderの秘密のファイルパス
var connectionString = File.Exists(secretFilePath)
    ? File.ReadAllText(secretFilePath).Trim() // Render上ではファイルから読み込む
    : builder.Configuration.GetConnectionString("DefaultConnection"); // ローカル開発用の予備設定

builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(connectionString));

var app = builder.Build();

// --- 起動時にDBを自動更新 ---
using (var scope = app.Services.CreateScope())
{
    var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
    db.Database.Migrate();
}

// --- 静的ファイル設定 ---
var audioDir = Path.Combine(app.Environment.ContentRootPath, "static/audio");
if (!Directory.Exists(audioDir)) Directory.CreateDirectory(audioDir);
app.UseStaticFiles(new StaticFileOptions { FileProvider = new Microsoft.Extensions.FileProviders.PhysicalFileProvider(Path.Combine(app.Environment.ContentRootPath, "static")) });

// --- 定数 ---
const int FRIENDSHIP_THRESHOLD = 5; // ★お友達になるために必要なチャット回数

// --- リクエストのデータ形式 ---
public record InteractRequest(Guid UserId, string UserName, string Message);

// --- メインエンドポイント ---
app.MapPost("/interact", async (AppDbContext db, Voicevox.Voicevox voicevox, [FromBody] InteractRequest req) =>
{
    string responseMessage;
    string audioUrl = "";
    int speakerId = 1; // デフォルトの声

    // ステップ1: ユーザーがお友達（登録済み）か確認
    var user = await db.Users.FindAsync(req.UserId);

    // --- お友達だった場合の処理 ---
    if (user != null)
    {
        speakerId = user.VoicevoxSpeakerId; // ユーザー固有の声設定を読み込む

        // コマンド処理
        if (req.Message.StartsWith("@"))
        {
            var parts = req.Message.Split(' ');
            if (parts[0] == "@config" && parts.Length > 2 && parts[1] == "voice")
            {
                user.VoicevoxSpeakerId = int.Parse(parts[2]);
                await db.SaveChangesAsync();
                responseMessage = $"承知いたしました。私の声をご指定のID:{user.VoicevoxSpeakerId}番に変更しますね。";
            }
            else
            {
                responseMessage = "申し訳ありません、そのコマンドは分かりかねます。";
            }
        }
        // 通常会話処理
        else
        {
            // ここでGemini APIと通信し、ユーザーの会話履歴(user.ChatHistoryJson)を使って応答を生成
            responseMessage = $"（ここにGeminiの応答が入ります）こんにちは、{user.UserName}様！";
            // ... Geminiとの通信後、user.ChatHistoryJsonを更新して保存 ...
        }
    }
    // --- 初めてのお客様だった場合の処理 ---
    else
    {
        // ステップ2: 受付名簿（ChatLogs）を確認
        var chatLog = await db.ChatLogs.FindAsync(req.UserId);
        if (chatLog == null) // 初めて話す人
        {
            chatLog = new ChatLog { UserId = req.UserId, UserName = req.UserName, InteractionCount = 1 };
            db.ChatLogs.Add(chatLog);
        }
        else // 以前に話したことがある人
        {
            chatLog.InteractionCount++;
        }
        await db.SaveChangesAsync();

        // ステップ3: 常連さん昇格チェック
        if (chatLog.InteractionCount >= FRIENDSHIP_THRESHOLD)
        {
            // お友達としてUsersテーブルに登録！
            var newUser = new User { UserId = req.UserId, UserName = req.UserName };
            db.Users.Add(newUser);
            db.ChatLogs.Remove(chatLog); // 受付名簿からは削除
            await db.SaveChangesAsync();
            
            responseMessage = $"いつもお話ししに来てくださり、ありがとうございます、{req.UserName}様！ とても嬉しいです。今日からあなたの専属コンシェルジュとして、会話を記憶させていただきますね！";
        }
        else
        {
            // まだゲストのお客様
             responseMessage = $"（ここにGeminiの応答が入ります）こんにちは、{req.UserName}さん。";
        }
    }

    // --- 共通の音声合成処理 ---
    try
    {
        var query = await voicevox.CreateAudioQueryAsync(responseMessage, speakerId);
        var wavData = await voicevox.SynthesisAsync(query, speakerId);
        var filename = $"{Guid.NewGuid()}.wav";
        var filepath = Path.Combine(audioDir, filename);
        await File.WriteAllBytesAsync(filepath, wavData);
        var baseUrl = Environment.GetEnvironmentVariable("RENDER_EXTERNAL_URL") ?? "http://localhost:5000";
        audioUrl = $"{baseUrl}/audio/{filename}";
    }
    catch(Exception ex)
    {
        Console.WriteLine($"音声合成エラー: {ex.Message}");
    }

    return Results.Ok(new { message = responseMessage, audio_url = audioUrl });
});

app.Run();
