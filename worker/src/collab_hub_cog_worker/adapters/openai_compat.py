"""One call to an OpenAI-compatible model, no tools: the plainest harness there is.

What a context Cog with no agent loop needs, and the proof that the seam is
harness-neutral: the same server and the same envelope as the ACP adapter,
with a chat completion behind it. Standard library only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from ..envelope import Cancelled, WorkerError
from ..limits import looks_like_overflow
from ..task import Context, Frame, Interaction, Prompt, Signal


class OpenAICompatAdapter:
    """``POST {base_url}/chat/completions``: the prompt, its Frames as a context message, the binding's limits."""

    def __init__(self, *, system: str | None = None, timeout: float = 600.0, temperature: float | None = None) -> None:
        self.system = system
        self.timeout = timeout
        self.temperature = temperature
        self._context: Context | None = None
        self._closed = False

    def start(self, context: Context) -> None:
        binding = context.binding
        if not binding.base_url:
            raise WorkerError("binding-invalid", "no model endpoint was delivered: the binding has no base_url")
        if not binding.model:
            raise WorkerError("binding-invalid", "no model name was delivered: the binding has no model")
        self._context = context

    def alive(self) -> bool:
        return not self._closed

    def cancel(self) -> None:
        pass  # a request in flight runs to its timeout; the signal is checked around it

    def close(self) -> None:
        self._closed = True

    def interact(self, prompt: Prompt, context: Context, signal: Signal) -> Interaction:
        if signal.is_set():
            raise Cancelled()
        binding = context.binding
        messages: list[dict[str, Any]] = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        if prompt.frames:
            messages.append({"role": "system", "content": self.frames_message(prompt.frames)})
        messages.append({"role": "user", "content": prompt.text})
        body: dict[str, Any] = {"model": binding.model, "messages": messages}
        if binding.max_output_tokens:
            body["max_tokens"] = binding.max_output_tokens
        if self.temperature is not None:
            body["temperature"] = self.temperature
        answer = self._post(f"{binding.base_url.rstrip('/')}/chat/completions", body, binding.api_key)
        if signal.is_set():
            raise Cancelled()
        try:
            choice = answer["choices"][0]
            text = choice["message"]["content"]
            if isinstance(text, list):  # content parts
                text = "".join(part.get("text", "") for part in text if isinstance(part, Mapping))
            if not isinstance(text, str):
                raise TypeError("content is not text")
        except (KeyError, IndexError, TypeError) as exc:
            raise WorkerError("model-response-malformed", f"the model's answer has no message content: {exc}") from None
        if not text.strip():
            raise WorkerError("model-response-malformed", "the model answered with no content")
        return Interaction(text=text.strip(), raw=json.dumps(answer)[:20000], usage=self._usage(answer),
                           stop=choice.get("finish_reason"))

    @staticmethod
    def frames_message(frames: tuple[Frame, ...]) -> str:
        parts = ["Frames: context the organization supplied for this task."]
        for frame in frames:
            parts.append(f"## {frame.name} ({frame.location})\n{frame.text}")
        return "\n\n".join(parts)

    @staticmethod
    def _usage(answer: Mapping[str, Any]) -> Mapping[str, Any] | None:
        usage = answer.get("usage")
        if not isinstance(usage, Mapping):
            return None
        return {"tokens": int(usage.get("total_tokens") or 0), "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0)}

    def _post(self, url: str, body: Mapping[str, Any], key: str | None) -> Mapping[str, Any]:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key or 'none'}"}
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - the bound endpoint
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:2000]
            if exc.code in (400, 413, 422) and looks_like_overflow(detail):
                raise WorkerError("context-exhausted", f"the model refused the input as too long: {detail}") from None
            if exc.code in (401, 403, 404):
                raise WorkerError("binding-invalid", f"the model endpoint answered HTTP {exc.code}: {detail}") from None
            raise WorkerError("model-call-failed", f"the model endpoint answered HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise WorkerError("model-unavailable", f"the model endpoint cannot be reached: {exc}") from None
        try:
            answer = json.loads(raw)
        except ValueError:
            raise WorkerError("model-response-malformed", "the model's answer is not JSON") from None
        if not isinstance(answer, Mapping):
            raise WorkerError("model-response-malformed", "the model's answer is not an object")
        return answer
