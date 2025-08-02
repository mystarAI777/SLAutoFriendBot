// 【最終手術版】Program.cs
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using SLVoicevoxServer.Data;
using VoicevoxClientSharp;
using System.Text.Json;

var builder = WebApplication.CreateBuilder(args);

var secretFilePath = "/etc/secrets/connection_string";
var connectionString = File.Exists(secretFilePath)
    ? File.ReadAllText(secretFilePath).Trim()
    : builder.Configuration.GetConnectionString("DefaultConnection");

builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(connectionString));

builder.Services.AddSingleton<IVoicevox>(sp => Voicevox.Create("127.0.0.1", 50021).Result);

var app = builder.Build();

using (var scope = app.Services.CreateScope())
{
    var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
    db.Database.Migrate();
}

var audioDir = Path.Combine(app.Environment.ContentRootPath, "static/audio");
if (!Directory.Exists(audioDir)) Directory.CreateDirectory(audioDir);
app.UseStaticFiles(new StaticFileOptions { FileProvider = new Microsoft.Extensions.FileProviders.PhysicalFileProvider(Path.Combine(app.Environment.ContentRootPath, "static")) });

// --- ★★★【健康診断】用の窓口を追加 ★★★ ---
app.MapGet("/healthz", () => Results.Ok(new { status = "ok" }));


app.MapPost("/interact", async (AppDbContext db, IVoicevox voicevox, [FromBody] InteractRequest req) =>
{
    string responseMessage;
    string audioUrl = "";
    // ★★★【スリム化】音声は「もちこさん(ID:9)」に固定 ★★★
    const int speakerId = 9; 

    // (ユーザー登録などのロジックは変更なし)
    var user = await db.Users.FindAsync(req.UserId);
    if (user != null)
    {
        if (req.Message.StartsWith("@")) { responseMessage = "申し訳ありません、このバージョンではコマンドは使用できません。"; }
        else { responseMessage = $"（AIの応答）こんにちは、{user.UserName}様！"; }
    }
    else
    {
        var chatLog = await db.ChatLogs.FindAsync(req.UserId);
        if (chatLog == null)
        {
            chatLog = new ChatLog { UserId = req.UserId, UserName = req.UserName };
            db.ChatLogs.Add(chatLog);
        }
        else { chatLog.InteractionCount++; }
        await db.SaveChangesAsync();

        if (chatLog.InteractionCount >= 5) // FRIENDSHIP_THRESHOLD
        {
            var newUser = new User { UserId = req.UserId, UserName = req.UserName };
            db.Users.Add(newUser);
            db.ChatLogs.Remove(chatLog);
            await db.SaveChangesAsync();
            responseMessage = $"いつもありがとうございます、{req.UserName}様！今日からあなたの専属コンシェルジュになりますね。声はずっと、もちこが担当します。";
        }
        else { responseMessage = $"（AIの応答）こんにちは、{req.UserName}さん。"; }
    }

    try
    {
        var query = await voicevox.CreateAudioQueryAsync(responseMessage, speakerId);
        var wavData = await voicevox.SynthesisAsync(query);
        var filename = $"{Guid.NewGuid()}.wav";
        var filepath = Path.Combine(audioDir, filename);
        await File.WriteAllBytesAsync(filepath, wavData);
        var baseUrl = Environment.GetEnvironmentVariable("RENDER_EXTERNAL_URL") ?? "http://localhost:5000";
        audioUrl = $"{baseUrl}/audio/{filename}";
    }
    catch (Exception ex) { Console.WriteLine($"音声合成エラー: {ex.Message}"); }
    
    return Results.Ok(new { message = responseMessage, audio_url = audioUrl });
});

app.Run();

public record InteractRequest(Guid UserId, string UserName, string Message);
