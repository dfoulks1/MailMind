"""
mailmind — background Gmail RAG service.

Import surface:
    from mailmind.config   import Settings
    from mailmind.models   import AnalysisMode, AnalysisResult, ...
    from mailmind.oauth    import OAuthTokenManager
    from mailmind.gmail    import GmailClient, ThreadParser
    from mailmind.ollama   import OllamaClient
    from mailmind.analysis import GmailAnalyzer
    from mailmind.rag      import RagStore
    from mailmind.scheduler import IngestionScheduler
    from mailmind.service  import MailMindService
"""

__version__ = "1.0.0"
