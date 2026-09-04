"""External provider integrations.

Rule 3: no external provider SDK (Clerk, S3-compatible storage, LLM/TTS
providers, ...) is imported anywhere outside this package. Everything else
talks to a provider through a module here, never directly.
"""
