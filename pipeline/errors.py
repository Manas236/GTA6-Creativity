"""Custom exception types for the pipeline."""
from __future__ import annotations


class PipelineError(Exception):
    """Base error for all pipeline failures."""
    pass


class DiscoveryError(PipelineError):
    """Raised when discovering items from YouTube, Reddit, or Twitch fails."""
    pass


class DownloadError(PipelineError):
    """Raised when video download or media validation fails."""
    pass


class DedupeError(PipelineError):
    """Raised during perceptual fingerprinting or deduplication matching."""
    pass


class CutError(PipelineError):
    """Raised during cut window selection or video trimming."""
    pass


class RenderError(PipelineError):
    """Raised during video compositing, whisper transcription, or ffmpeg render."""
    pass


class PublishError(PipelineError):
    """Raised during Meta Graph API publication."""
    pass


class RateLimitError(PipelineError):
    """Raised when an external API issues a 429 or rate-limiting block."""

    def __init__(self, source: str, pause_seconds: float = 3600.0, message: str = ""):
        super().__init__(f"Rate limit / block on {source}: {message} (pause {pause_seconds:.0f}s)")
        self.source = source
        self.pause_seconds = pause_seconds

