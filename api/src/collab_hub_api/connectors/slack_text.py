from __future__ import annotations

import re
import unicodedata

# Slack message text embeds links as ``<target|label>`` angle-bracket entities. This
# module renders that markup as readable text and keeps the exact http(s) target of
# every link, so the model can cite or follow it: Slack shortens long labels with an
# ellipsis, so a label alone cannot recover its target (#143). Only http and https
# targets are kept; any other scheme (javascript:, data:, file:, tel:, slack://, ...)
# is reduced to its label or ``[link]``, and so is an http(s) target containing
# whitespace, control or format characters, ``<``, ``>``, or quotes. Plain URLs
# typed into the message get the same character check and become ``[link]`` when it
# fails. URLs inside a label are replaced with ``[link]`` and the real target is
# always shown after the label, so a label cannot hide where a link goes; a label
# without a scheme or ``www.`` (such as ``paypal.com/login``) is still shown as
# written. Links were once stripped entirely to work around an Apollo renderer crash
# on link-shaped text (apollo-desktop#365); that crash was fixed in apollo-desktop
# PR #414.

_LINK_PLACEHOLDER = "[link]"
_TRAILING_PUNCTUATION = ").,;:!?]}>\"'"

# Slack wraps links and mentions in angle brackets: <target>, <target|label>. Real
# ``<`` / ``>`` in message text arrive HTML-escaped (``&lt;`` / ``&gt;``), so a bare
# ``<...>`` is always Slack markup, never literal user text.
_SLACK_ENTITY = re.compile(r"<(?P<target>[^<>|]*)(?:\|(?P<label>[^<>]*))?>")
_HTTP_SCHEME = re.compile(r"https?://", re.IGNORECASE)
# URLs typed into the message, or inside a link label.
_PLAIN_URL = re.compile(r"(?:https?://|www\.)[^\s<>|]+", re.IGNORECASE)
# One pass over the message: entities are rendered and plain URLs are checked only
# in the text between them, so a rendered entity is never processed twice.
_SLACK_TOKEN = re.compile(f"(?P<entity>{_SLACK_ENTITY.pattern})|(?P<plain>{_PLAIN_URL.pattern})", re.IGNORECASE)
_UNSAFE_URL_CHARACTERS = frozenset("<>\"'")
# Control (Cc) and format (Cf) characters: the latter include bidi overrides such as
# U+202E and zero-width characters such as U+200B, which can disguise a URL.
_UNSAFE_URL_CATEGORIES = frozenset({"Cc", "Cf"})


def _http_target(target: str) -> str | None:
    """Return the exact http(s) URL for a link target, or ``None`` to neutralize it."""
    if not _HTTP_SCHEME.match(target):
        return None
    return _safe_url(target)


def _safe_url(url: str) -> str | None:
    """Return ``url`` with ``&amp;`` unescaped, or ``None`` if it has an unsafe character."""
    # Slack escapes ``&`` in message text, including inside URLs.
    url = url.replace("&amp;", "&")
    for char in url:
        if char.isspace() or char in _UNSAFE_URL_CHARACTERS or unicodedata.category(char) in _UNSAFE_URL_CATEGORIES:
            return None
    return url


def _comparable_url(url: str) -> str:
    return _HTTP_SCHEME.sub("", url.replace("&amp;", "&"), count=1).rstrip("/").lower()


def _render_entity(match: re.Match[str]) -> str:
    target = match.group("target") or ""
    label = (match.group("label") or "").strip()
    if target.startswith("@"):
        # User/bot mention: <@U123> or <@U123|name>.
        return label or target
    if target.startswith("#"):
        # Channel mention: <#C123|general> -> #general (fall back to the raw id).
        return f"#{label}" if label else target
    if target.startswith("!"):
        # Special mention: <!here>, <!channel>, <!subteam^S1|team>.
        if label:
            return f"@{label}"
        return f"@{target[1:].split('^', 1)[0]}"
    url = _http_target(target)
    if url is not None:
        # Auto-links repeat the target as the label (<https://x.test|x.test>).
        if not label or _comparable_url(label) == _comparable_url(url):
            return url
        return f"{_neutralize_urls(label)} ({url})"
    # Any other target (mailto, tel, javascript, slack action links, unsafe http)
    # keeps only its label.
    if label:
        return _neutralize_urls(label)
    if target.lower().startswith("mailto:"):
        return target[len("mailto:") :] or _LINK_PLACEHOLDER
    return _LINK_PLACEHOLDER


def _neutralize_urls(label: str) -> str:
    return _PLAIN_URL.sub(lambda match: _LINK_PLACEHOLDER + _split_trailing(match.group(0))[1], label)


def _split_trailing(url: str) -> tuple[str, str]:
    """Split trailing punctuation (``)``, ``.``, closing quotes, ...) off a matched URL."""
    end = len(url)
    while end and url[end - 1] in _TRAILING_PUNCTUATION:
        end -= 1
    return url[:end], url[end:]


def _render_plain_url(match: re.Match[str]) -> str:
    # Strip trailing punctuation first, so a closing quote is not taken as part of
    # the URL and rejected as unsafe.
    url, trailing = _split_trailing(match.group(0))
    return (_safe_url(url) or _LINK_PLACEHOLDER) + trailing


def _render_token(match: re.Match[str]) -> str:
    if match.group("entity") is not None:
        return _render_entity(match)
    return _render_plain_url(match)


def sanitize_slack_text(text: str) -> str:
    """Return ``text`` with Slack entities rendered as readable text.

    User/channel mentions keep their human-readable form (``@name``, ``#general``).
    An http(s) link keeps its exact target, after its label when the label differs
    (``label (url)``); any other link is replaced with its label, or ``[link]`` when
    there is none. Plain URLs typed into the message are kept with ``&amp;``
    unescaped, or replaced with ``[link]`` when they contain an unsafe character.
    """
    if not text:
        return text
    return _SLACK_TOKEN.sub(_render_token, text)
