// 【修正版】Program.cs - 最新のVoicevoxClientSharp APIに対応
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using SLVoicevoxServer.Data;
using SLVoicevoxServer.Config; // もち子さん設定クラス
using VoicevoxClientSharp; // VoicevoxClientSharp NuGetパッケージが必要
using System.Text.Json;

var builder = WebApplication.CreateBuilder(args);

var secretFilePath = "/etc/secrets/connection_string";
var connectionString = File.Exists(secretFilePath)
    ? File.ReadAllText(secretFilePath).Trim()
    : builder.Configuration.GetConnectionString("DefaultConnection");

builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(connectionString));

// もち子さん専用VoicevoxSynthesizerの設定
builder.Services.AddSingleton<VoicevoxSynthesizer>(sp =>
{
    // デフォルトでhttp://localhost:50021に接続（もち子さん専用）
    var synthesizer = new VoicevoxSynthesizer();
    
    // もち子さん（スピーカーID: 9）を事前初期化（オプション）
    // 初回の音声合成時間を短縮できます
    Task.Run(async () =>
    {
        try
        {
            await synthesizer.InitializeStyleAsync(MochikoVoiceConfig.SPEAKER_ID); // もち子さんのスタイルを初期化
            Console.WriteLine($"{MochikoVoiceConfig.SPEAKER_NAME}さんのスタイルを初期化しました");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"{MochikoVoiceConfig.SPEAKER_NAME}さんのスタイル初期化エラー: {ex.Message}");
        }
    });
    
    return synthesizer;
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

    var user = await db.Users.FindAsync(req.UserId);
    if (user != null)
    {
        if (req.Message.StartsWith("@")) 
        { 
            responseMessage = MochikoVoiceConfig.Messages.COMMAND_NOT_SUPPORTED;
        }
        else 
        { 
            responseMessage = string.Format(MochikoVoiceConfig.Messages.GREETING_REGISTERED, user.UserName);
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
            responseMessage = string.Format(MochikoVoiceConfig.Messages.WELCOME_NEW_USER, req.UserName);
        }
        else 
        { 
            responseMessage = string.Format(MochikoVoiceConfig.Messages.GREETING_GUEST, req.UserName);
        }
    }

    try
    {
        // もち子さんの声で音声合成を実行
        var synthesisResult = await synthesizer.SynthesizeSpeechAsync(
            MochikoVoiceConfig.SPEAKER_ID, 
            responseMessage,
            speedScale: MochikoVoiceConfig.DefaultParameters.SpeedScale,
            pitchScale: MochikoVoiceConfig.DefaultParameters.PitchScale,
            intonationScale: MochikoVoiceConfig.DefaultParameters.IntonationScale,
            volumeScale: MochikoVoiceConfig.DefaultParameters.VolumeScale
        );
        
        var filename = $"{MochikoVoiceConfig.AUDIO_FILE_PREFIX}_{Guid.NewGuid()}.wav";
        var filepath = Path.Combine(audioDir, filename);
        await File.WriteAllBytesAsync(filepath, synthesisResult.Wav);
        
        var baseUrl = Environment.GetEnvironmentVariable("RENDER_EXTERNAL_URL") ?? "http://localhost:5000";
        audioUrl = $"{baseUrl}/audio/{filename}";
        
        Console.WriteLine($"{MochikoVoiceConfig.SPEAKER_NAME}さんの音声を生成しました: {filename}");
    }
    catch (Exception ex) 
    { 
        Console.WriteLine($"{MochikoVoiceConfig.SPEAKER_NAME}さんの音声合成エラー: {ex.Message}"); 
    }
    
    return Results.Ok(new { message = responseMessage, audio_url = audioUrl });
});

app.Run();

public record InteractRequest(Guid UserId, string UserName, string Message);
