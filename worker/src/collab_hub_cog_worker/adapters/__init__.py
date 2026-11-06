"""The adapters the SDK ships: any ACP agent over stdio, and a direct OpenAI-compatible model call."""

from .acp import AcpHarnessAdapter
from .openai_compat import OpenAICompatAdapter

__all__ = ["AcpHarnessAdapter", "OpenAICompatAdapter"]
