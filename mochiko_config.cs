// MochikoVoiceConfig.cs - もち子さん専用の音声設定
namespace SLVoicevoxServer.Config
{
    /// <summary>
    /// もち子さん専用の音声設定
    /// </summary>
    public static class MochikoVoiceConfig
    {
        /// <summary>
        /// もち子さんのスピーカーID
        /// </summary>
        public const int SPEAKER_ID = 9;
        
        /// <summary>
        /// もち子さんの表示名
        /// </summary>
        public const string SPEAKER_NAME = "もち子";
        
        /// <summary>
        /// 音声ファイルのプレフィックス
        /// </summary>
        public const string AUDIO_FILE_PREFIX = "mochiko";
        
        /// <summary>
        /// デフォルトの音声パラメータ
        /// </summary>
        public static class DefaultParameters
        {
            /// <summary>
            /// 話速（1.0が標準）
            /// </summary>
            public const decimal SpeedScale = 1.0m;
            
            /// <summary>
            /// 音高（0.0が標準）
            /// </summary>
            public const decimal PitchScale = 0.0m;
            
            /// <summary>
            /// 抑揚（1.0が標準）
            /// </summary>
            public const decimal IntonationScale = 1.0m;
            
            /// <summary>
            /// 音量（1.0が標準）
            /// </summary>
            public const decimal VolumeScale = 1.0m;
        }
        
        /// <summary>
        /// もち子さん専用メッセージ
        /// </summary>
        public static class Messages
        {
            public const string WELCOME_NEW_USER = "いつもありがとうございます、{0}様！今日からあなたの専属コンシェルジュになりますね。声はずっと、もち子が担当します。";
            public const string GREETING_REGISTERED = "（AIの応答）こんにちは、{0}様！";
            public const string GREETING_GUEST = "（AIの応答）こんにちは、{0}さん。";
            public const string COMMAND_NOT_SUPPORTED = "申し訳ありません、このバージョンではコマンドは使用できません。";
        }
    }
}