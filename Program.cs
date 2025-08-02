using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using SLVoicevoxServer.Data;
using VoicevoxClientSharp; // ★修正点１：正しい住所(using)に変更
using System.Text.Json;

// --- 初期設定 ---
var builder = WebApplication.CreateBuilder(args);

// --- データベース設定（Secret Fileから読み込む方式） ---
var secretFilePath = "/etc/secrets/connection_string";
var connectionString = File.Exists(secretFilePath)
    ? File.ReadAllText(secretFilePath).Trim()
    : builder.Configuration.GetConnectionString("DefaultConnection");

builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(connectionString));

// --- DIコンテナにサービスを登録 ---
// ★修正点２：正しいクラス名で登録
builder.Services.AddScoped<Voicevox>(_ => new Voicevox("127.0.0.1", 50021));

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
const int FRIENDSHIP_THRESHOLD = 5;

// --- メインエンドポイント ---
// ★修正点３：正しいクラス名で受け取る
app.MapPost("/interact", async (AppDbContext db, Voicevox voicevox, [FromBody] InteractRequest req) =>
{
    string responseMessage;
    string audioUrl = "";
    int speakerId = 1;

    var user = await db.Users.FindAsync(req.UserId);

    if (user != null)
    {
        speakerId = user.VoicevoxSpeakerId;
        if (req.Message.StartsWith("@"))
        {
            var parts = req.Message.Split(' ');
            if (parts.Length > 2 && parts[0] == "@config" && parts[1] == "voice")
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
        else
        {
            responseMessage = $"（AIの応答）こんにちは、{user.UserName}様！";
        }
    }
    else
    {
        var chatLog = await db.ChatLogs.FindAsync(req.UserId);
        if (chatLog == null)
        {
            chatLog = new ChatLog { UserId = req.UserId, UserName = req.UserName };
            db.ChatLogs.Add(chatLog);
        }
        else
        {
            chatLog.InteractionCount++;
        }
        await db.SaveChangesAsync();

        if (chatLog.InteractionCount >= FRIENDSHIP_THRESHOLD)
        {
            var newUser = new User { UserId = req.UserId, UserName = req.UserName };
            db.Users.Add(newUser);
            db.ChatLogs.Remove(chatLog);
            await db.SaveChangesAsync();
            responseMessage = $"いつもお話ししに来てくださり、ありがとうございます、{req.UserName}様！ とても嬉しいです。今日からあなたの専属コンシェルジュとして、会話を記憶させていただきますね！";
        }
        else
        {
            responseMessage = $"（AIの応答）こんにちは、{req.UserName}さん。";
        }
    }

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
    catch (Exception ex)
    {
        Console.WriteLine($"音声合成エラー: {ex.Message}");
    }

    return Results.Ok(new { message = responseMessage, audio_url = audioUrl });
});

app.Run();

public record InteractRequest(Guid UserId, string UserName, string Message);
