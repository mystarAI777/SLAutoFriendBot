// 【修正版】Program.cs
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using SLVoicevoxServer.Data;
using SLVoicevoxServer.Config; // もち子さん設定クラス
using VoicevoxClientSharp; // VoicevoxClientSharp NuGetパッケージ
using System.Text.Json;

var builder = WebApplication.CreateBuilder(args);

var secretFilePath = "/etc/secrets/connection_string";
var connectionString = File.Exists(secretFilePath)
    ? File.ReadAllText(secretFilePath).Trim()
    : builder.Configuration.GetConnectionString("DefaultConnection");

builder.Services.AddDbContext<AppDbContext>(options =>
    options.UseNpgsql(connectionString));

// もち子さん専用VoicevoxSynthesizerの設定
builder.Services.AddSingleton<VoicevoxSynthesizer?>(sp =>
{
    try
    {
        var voicevoxUrl = Environment.GetEnvironmentVariable("VOICEVOX_URL") ?? "http://127.0.0.1:50021";
        var httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(10) }; // タイムアウトを少し延長
        var apiClient = Voicevox.Create(baseUri: voicevoxUrl, httpClient).Result;
        var synthesizer = new VoicevoxSynthesizer(apiClient);
        
        // バックグラウンドで初期化
        Task.Run(async () => {
            try {
                await synthesizer.InitializeStyleAsync(MochikoVoiceConfig.SPEAKER_ID);
                Console.WriteLine($"{MochikoVoiceConfig.SPEAKER_NAME}さんのスタイルを初期化しました");
            } catch (Exception ex) {
                Console.WriteLine($"{MochikoVoiceConfig.SPEAKER_NAME}さんのスタイル初期化エラー: {ex.Message}");
            }
        });
        return synthesizer;
    }
    catch (Exception ex)
    {
        Console.WriteLine($"VOICEVOXエンジンとの接続に失敗しました: {ex.Message}");
        return null; // 失敗してもアプリは続行
    }
});

var app = builder.Build();

using (var scope = app.Services.CreateScope())
{
    var db = scope.ServiceProvider.GetRequiredService<AppDbContext>();
    db.Database.Migrate();
}

var audioDir = Path.Combine(app.Environment.ContentRootPath, "static/audio");
if (!Directory.Exists(audioDir)) Directory.CreateDirectory(audioDir);
app.UseStaticFiles(new StaticFileOptions { FileProvider = new Microsoft.Extensions.FileProviders.PhysicalFileProvider(Path.Combine(app.Environment.ContentRootPath, "static")) });

app.MapGet("/healthz", () => Results.Ok(new { status = "ok" }));

app.MapPost("/interact", async (AppDbContext db, VoicevoxSynthesizer? synthesizer, [FromBody] InteractRequest req) =>
{
    string responseMessage;
    string audioUrl = "";

    var user = await db.Users.FindAsync(req.UserId);
    if (user != null)
    {
        if (req.Message.StartsWith("@")) { responseMessage = MochikoVoiceConfig.Messages.COMMAND_NOT_SUPPORTED; }
        else { responseMessage = string.Format(MochikoVoiceConfig.Messages.GREETING_REGISTERED, user.UserName); }
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

        if (chatLog.InteractionCount >= 5)
        {
            var newUser = new User { UserId = req.UserId, UserName = req.UserName };
            db.Users.Add(newUser);
            db.ChatLogs.Remove(chatLog);
            await db.SaveChangesAsync();
            responseMessage = string.Format(MochikoVoiceConfig.Messages.WELCOME_NEW_USER, req.UserName);
        }
        else { responseMessage = string.Format(MochikoVoiceConfig.Messages.GREETING_GUEST, req.UserName); }
    }

    if (synthesizer != null)
    {
        try
        {
            var synthesisResult = await synthesizer.SynthesizeSpeechAsync(
                MochikoVoiceConfig.SPEAKER_ID, responseMessage,
                speedScale: MochikoVoiceConfig.DefaultParameters.SpeedScale,
                pitchScale: MochikoVoiceConfig.DefaultParameters.PitchScale
            );
            var filename = $"{MochikoVoiceConfig.AUDIO_FILE_PREFIX}_{Guid.NewGuid()}.wav";
            var filepath = Path.Combine(audioDir, filename);
            await File.WriteAllBytesAsync(filepath, synthesisResult.Wav);
            var baseUrl = Environment.GetEnvironmentVariable("RENDER_EXTERNAL_URL") ?? "http://localhost:5000";
            audioUrl = $"{baseUrl}/audio/{filename}";
        }
        catch (Exception ex) { Console.WriteLine($"{MochikoVoiceConfig.SPEAKER_NAME}さんの音声合成エラー: {ex.Message}"); }
    }
    
    return Results.Ok(new { message = responseMessage, audio_url = audioUrl });
});

app.Run();

public record InteractRequest(Guid UserId, string UserName, string Message);
