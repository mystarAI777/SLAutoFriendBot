using System.ComponentModel.DataAnnotations;
namespace SLVoicevoxServer.Data;
public class User
{
    [Key]
    public Guid UserId { get; set; }
    public string? UserName { get; set; }
    public int VoicevoxSpeakerId { get; set; } = 1;
    public string ChatHistoryJson { get; set; } = "[]";
}