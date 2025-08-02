using System.ComponentModel.DataAnnotations;
namespace SLVoicevoxServer.Data;
public class ChatLog
{
    [Key]
    public Guid UserId { get; set; }
    public string? UserName { get; set; }
    public int InteractionCount { get; set; } = 1;
}