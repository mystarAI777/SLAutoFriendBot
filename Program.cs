// 【修正版】Program.cs - 最新のVoicevoxClientSharp APIに対応
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using SLVoicevoxServer.Data;
using VoicevoxClientSharp; // VoicevoxClientSharp NuGetパッケージが必要
using System.Text.Json;

var builder = WebApplication.CreateBuilder(args);

var secretFilePath = "/etc/secrets/connection_string";
var connectionString = File.Exists(secretFilePath)
    ? File.ReadAllText(secretFilePath).Trim()
    : builder.Configuration.GetConnectionString("DefaultConnection");

builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(connectionString));

// 最新のVoicevoxClientSharpに対応した実装
builder.Services.AddSingleton<VoicevoxSynthesizer>(sp =>
{
    // デフォルトでhttp://localhost:50021に接続
    return new VoicevoxSynthesizer();
});

var app = builder.Build();

using (var scope = app.Services.CreateScope())
{
    var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
    db.Database.Migrate();
}

var audioDir = Path.Combine(app.Environment.ContentRootPath, "static/audio");
if (!Directory.Exists(audioDir)) Directory.CreateDirectory(audioDir);
app.UseStaticFiles(new StaticFileOptions { 
    FileProvider = new Microsoft.Extensions.FileProviders.PhysicalFileProvider(
        Path.Combine(app.Environment.ContentRootPath, "static")) 
});

// 「健康診断」用の窓口
app.MapGet("/healthz", () => Results.Ok(new { status = "ok" }));

app.MapPost("/interact", async (AppDbContext db, VoicevoxSynthesizer synthesizer, [FromBody] InteractRequest req) =>
{
    string responseMessage;
    string audioUrl = "";
    const int speakerId = 9; // もちこさん固定

    var user = await db.Users.FindAsync(req.UserId);
    if (user != null)
    {
        if (req.Message.StartsWith("@")) 
        { 
            responseMessage = "申し訳ありません、このバージョンではコマンドは使用できません。"; 
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

        if (chatLog.InteractionCount >= 5)
        {
            var newUser = new User { UserId = req.UserId, UserName = req.UserName };
            db.Users.Add(newUser);
            db.ChatLogs.Remove(chatLog);
            await db.SaveChangesAsync();
            responseMessage = $"いつもありがとうございます、{req.UserName}様！今日からあなたの専属コンシェルジュになりますね。声はずっと、もちこが担当します。";
        }
        else 
        { 
            responseMessage = $"（AIの応答）こんにちは、{req.UserName}さん。"; 
        }
    }

    try
    {
        // 最新のVoicevoxClientSharpのAPIを使用
        var synthesisResult = await synthesizer.SynthesizeSpeechAsync(speakerId, responseMessage);
        
        var filename = $"{Guid.NewGuid()}.wav";
        var filepath = Path.Combine(audioDir, filename);
        await File.WriteAllBytesAsync(filepath, synthesisResult.Wav);
        
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
