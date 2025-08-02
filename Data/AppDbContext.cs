using Microsoft.EntityFrameworkCore;
namespace SLVoicevoxServer.Data;
public class AppDbContext : DbContext
{
    public AppDbContext(DbContextOptions<AppDbContext> options) : base(options) { }
    public DbSet<User> Users { get; set; }
    public DbSet<ChatLog> ChatLogs { get; set; }
}