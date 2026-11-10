"""The Collab Hub home page (``/web/home``).

What a person sees at the hub's address: the product's front door. Signed
out, it offers sign-in and sign-up, both of which come back here; signed in,
it names the person and puts the web client, the desktop download, Frames,
the docs and the account page one click away.

Every link comes from configuration, never the request: the web client and
Frames from the hub's domain (:func:`hub_domain`), the rest from ``web.*``
settings checked at startup. Like the rest of the surface the page runs no
script: its look is one stylesheet (:data:`HOME_STYLESHEET`), the Collab mark
(:data:`COLLAB_MARK_SVG`) and the fonts in ``web/fonts``, all served from this
origin. Motion is CSS that plays once, on load.

Colour is blue and black only, in solid fills: no gradients. Surface colours
follow the system theme, light and dark. Connector logos are Simple
Icons 16.35.0 (CC0), inlined.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from .session import WebSession
from .surface import (
    ACCOUNT_PATH,
    BILLING_PATH,
    DATA_STATEMENT_PATH,
    HOME_FONT_PATHS,
    HOME_MARK_PATH,
    HOME_PATH,
    HOME_SCRIPT_PATH,
    HOME_STYLE_PATH,
    HOME_THEME_SCRIPT_PATH,
    PRIVACY_PATH,
    SIGNIN_PATH,
    SIGNOUT_PATH,
    TERMS_PATH,
    WebSurface,
)


def hub_domain(surface: WebSurface) -> str:
    """The configured domain, or the issuer's host without ``keycloak.``."""

    if surface.hub_domain:
        return surface.hub_domain
    host = urlsplit(surface.issuer_url).hostname or ""
    return host.removeprefix("keycloak.")


# Line icons, drawn inline: CSP allows inline SVG markup (it is not script) and
# `currentColor` lets the stylesheet colour them.
_ICON_PATHS = {
    "browser": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01"/>',
    "desktop": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    "frames": '<path d="M12 3l9 4.5-9 4.5-9-4.5z"/><path d="M3 12l9 4.5 9-4.5"/><path d="M3 16.5L12 21l9-4.5"/>',
    "docs": '<path d="M4 5a2 2 0 0 1 2-2h6v18H6a2 2 0 0 0-2 2zM20 5a2 2 0 0 0-2-2h-6v18h6a2 2 0 0 1 2 2z"/>',
    "account": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "repeat": '<path d="M4 12a8 8 0 0 1 14-5.3L20 9M20 4v5h-5M20 12a8 8 0 0 1-14 5.3L4 15M4 20v-5h5"/>',
    "files": '<path d="M8 3h7l4 4v10a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path d="M15 3v4h4M4 7v13a1 1 0 0 0 1 1h10"/>',
    "exit": '<path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4M10 16l-4-4 4-4M6 12h10"/>',
    "server": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "chat": '<path d="M4 5h16v11H9l-5 4z"/>',
    "history": '<path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 2"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 4.8 1c0 1.7-2.3 2-2.3 3.5M12 17h.01"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "logout": '<path d="M9 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h3M15 16l4-4-4-4M19 12H9"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    "sidebar": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/>',
    "card": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18M7 15h4"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "plug": '<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0zM12 17v5"/>',
    "spark": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>',
}

# Simple Icons 16.35.0 (CC0): the connectors this hub ships.
# The tools Collab connects to, and their logo files in ``web/connectors``
# (see SOURCE.txt there). GitHub's mark is near-black, so dark mode inverts it.
_CONNECTORS = (
    ("Gmail", "gmail"),
    ("Google Drive", "google-drive"),
    ("Google Calendar", "google-calendar"),
    ("Slack", "slack"),
    ("GitHub", "github"),
    ("Notion", "notion"),
    ("SharePoint", "sharepoint"),
)
_CONNECTOR_FILES = dict(_CONNECTORS)


# The Apple logo, for the macOS download (Remix Icon 1.2.10, apple-fill, Apache 2.0).
_APPLE_LOGO = '<path fill="currentColor" d="M11.673 7.222c-.876 0-2.232-.996-3.66-.96c-1.884.024-3.612 1.092-4.584 2.784c-1.956 3.396-.504 8.412 1.404 11.172c.936 1.344 2.04 2.856 3.504 2.808c1.404-.06 1.932-.912 3.636-.912c1.692 0 2.172.912 3.66.876c1.512-.024 2.472-1.368 3.396-2.724c1.068-1.56 1.512-3.072 1.536-3.156c-.036-.012-2.94-1.128-2.976-4.488c-.024-2.808 2.292-4.152 2.4-4.212c-1.32-1.932-3.348-2.148-4.056-2.196c-1.848-.144-3.396 1.008-4.26 1.008m3.12-2.832c.78-.936 1.296-2.244 1.152-3.54c-1.116.048-2.46.744-3.264 1.68c-.72.828-1.344 2.16-1.176 3.432c1.236.096 2.508-.636 3.288-1.572"/>'


def _mark(css_class: str = "icon-sm") -> str:
    """The Collab mark in the text colour, for buttons whose ground is blue."""

    paths = "".join(f'<path d="{d}"/>' for d, _ in _MARK_QUADRANTS)
    # The quadrants span roughly 10..54 of the 64 grid; frame them tightly so
    # the mark sits at the same optical size as the line icons beside it.
    return f'<svg class="{css_class}" viewBox="6 4 56 56" fill="currentColor" aria-hidden="true">{paths}</svg>'


def _apple(css_class: str = "icon-sm") -> str:
    return f'<svg class="{css_class}" viewBox="0 0 24 24" aria-hidden="true">{_APPLE_LOGO}</svg>'


def _icon(name: str, css_class: str = "icon") -> str:
    return (
        f'<svg class="{css_class}" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        ' stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f"{_ICON_PATHS[name]}</svg>"
    )


def _brand(root: str, name: str) -> str:
    """One connector's logo, in its own colours."""

    file = _CONNECTOR_FILES[name]
    mono = " mono" if file == "github" else ""
    return f'<img class="brand-icon{mono}" src="{root}/web/connectors/{file}.svg" alt="" width="20" height="20">'


def _connectors(root: str) -> str:
    """The tools strip: logos only, each named for screen readers and on hover."""

    return "".join(
        f'<li title="{html.escape(name)}">'
        + _brand(root, name)
        .replace('alt=""', f'alt="{html.escape(name)}"')
        .replace('class="brand-icon', 'class="strip-logo')
        + "</li>"
        for name, _ in _CONNECTORS
    )


# Door rows whose glyph is a brand mark: the Collab mark in its own blues
# (via the served file, so it matches the logo), and the Apple logo.
_DOOR_GLYPHS = {
    "collab": lambda: f'<img class="icon mark-img" src="{HOME_MARK_PATH}" alt="" width="22" height="22">',
    "apple": lambda: _apple("icon"),
}


def _door(icon: str, title: str, description: str, url: str) -> str:
    return (
        f'<a class="door" href="{html.escape(url)}">{_DOOR_GLYPHS.get(icon, lambda: _icon(icon))()}'
        f'<span class="door-text"><strong>{html.escape(title)}</strong><span>{html.escape(description)}</span></span>'
        f"{_icon('arrow', 'icon-sm door-arrow')}</a>"
    )


# The generated avatar's palette: dark grounds and blues, nothing else.
_AVATAR_GROUNDS = ("#0b1640", "#111114", "#0f1d52", "#18181b")
_AVATAR_BLUES = ("#1a3bb8", "#2952e3", "#4d75fe", "#7d9bff", "#a9bbff")


def _avatar(seed: str) -> str:
    """A default avatar drawn from *seed* (the person's subject): the same
    person always gets the same picture, and different people different ones.

    Three shapes on a dark ground, placed and coloured from a hash of the seed,
    echoing the Collab mark's circles and quarters.
    """

    h = hashlib.sha256(seed.encode()).digest()
    ground = _AVATAR_GROUNDS[h[0] % len(_AVATAR_GROUNDS)]
    blues = [_AVATAR_BLUES[b % len(_AVATAR_BLUES)] for b in h[1:4]]
    cx, cy = 8 + h[4] % 16, 8 + h[5] % 16
    r = 9 + h[6] % 6
    qx, qy = (0, 32)[h[7] % 2], (0, 32)[h[8] % 2]
    dot_x, dot_y = 6 + h[9] % 20, 6 + h[10] % 20
    return (
        '<svg class="avatar" viewBox="0 0 32 32" aria-hidden="true">'
        f'<rect width="32" height="32" fill="{ground}"/>'
        f'<circle cx="{qx}" cy="{qy}" r="22" fill="{blues[0]}"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{blues[1]}" fill-opacity="0.85"/>'
        f'<circle cx="{dot_x}" cy="{dot_y}" r="3.5" fill="{blues[2]}"/>'
        "</svg>"
    )


# The four views of the clickable Collab window: id, tab label, and the top
# edge of that view's sidebar entry in the screenshots, as % of their height,
# measured from the captures' pixels so the clickable area sits exactly on
# the entry. Every entry spans the same box horizontally (_SIDEBAR_ENTRY).
_VIEWS = (
    ("chat", "Chat", 24.78),
    ("frames", "Frames", 15.78),
    ("hub", "Intelligence Hub", 6.33),
    ("hub-usage", "Hub Usage", 10.33),
)
_SIDEBAR_ENTRY = {"left": 0.56, "width": 16.59, "height": 3.56}


def _shot(root: str, view: str, alt: str, css_class: str = "shot") -> str:
    """One real Collab screenshot, in the viewer's theme."""

    base = f"{root}/web/shots/{view}"
    return (
        f'<span class="{css_class}">'
        f'<img class="in-light" src="{base}-light.jpg" alt="{html.escape(alt)}" width="1440" height="900" loading="lazy">'
        f'<img class="in-dark" src="{base}-dark.jpg" alt="{html.escape(alt)}" width="1440" height="900" loading="lazy">'
        "</span>"
    )


# The model picker: provider, logo file (None draws a lettered tile), models.
_MODELS = (
    ("Anthropic", "anthropic", ("Claude Fable 5.2", "Claude Opus 5.5")),
    ("OpenAI", "openai", ("GPT-6.1 Terra", "GPT-6.1 Sol")),
    ("Google", "gemini", ("Gemini 4 Argon", "Gemini 3.8 Flash")),
    ("xAI", "xai", ("Grok 4.7", "Grok 4.6")),
    ("Meta", "meta", ("Muse Spark", "Muse Glimmer")),
    ("Mistral", "mistral", ("Mistral Large 4", "Mistral Medium 3.5")),
    ("DeepSeek", "deepseek", ("DeepSeek V4 Pro", "DeepSeek V4 Flash")),
    ("Qwen", "qwen", ("Qwen3.8 Max", "Qwen3.8 Flash")),
    ("Moonshot", "moonshot", ("Kimi K3.1 Pro", "Kimi K3")),
    ("Z.ai", "zai", ("GLM-5.3", "GLM-5.3 Flash")),
    ("MiniMax", "minimax", ("MiniMax M3.1 Flash", "MiniMax M3")),
    ("Cohere", "cohere", ("Command A+", "Command A")),
    ("NVIDIA", "nvidia", ("Nemotron 3.5 Super", "Nemotron 3 Ultra")),
    ("Thinking Machines", None, ("Your Tinker fine-tune",)),
)
_DEFAULT_MODEL = ("Claude Fable 5.2", "anthropic")
_MONO_LOGOS = ("openai", "xai", "moonshot", "zai")


def _logo(root: str, logo: str | None) -> str:
    if logo is None:
        return '<span class="logo tile">TM</span>'
    mono = " mono" if logo in _MONO_LOGOS else ""
    return f'<img class="logo{mono}" src="{root}/web/models/{logo}.svg" alt="" width="16" height="16">'


def _model_menu(root: str) -> str:
    groups = []
    for provider, logo, models in _MODELS:
        options = "".join(
            f'<button type="button" class="model-opt{" on" if model == _DEFAULT_MODEL[0] else ""}" role="menuitemradio"'
            f' data-model="{html.escape(model)}">{_logo(root, logo)}<span>{html.escape(model)}</span></button>'
            for model in models
        )
        groups.append(f'<div class="model-group"><p>{html.escape(provider)}</p>{options}</div>')
    return (
        '<div class="model-menu" data-menu role="menu" hidden>'
        '<p class="menu-note">Served through your hub\'s gateway</p>'
        f"{''.join(groups)}</div>"
    )


# Each view's icon, as the Collab sidebar shows it; the tabs above the window use the same.
_VIEW_ICONS = {"chat": "chat", "frames": "frames", "hub": "server", "hub-usage": "chart"}


# The chat's connections: display name, short name for the button, on at first.
_CHAT_CONNECTIONS = (
    ("Google Drive", "Drive", True),
    ("Gmail", "Gmail", True),
    ("Google Calendar", "Calendar", True),
    ("Slack", "Slack", False),
    ("GitHub", "GitHub", False),
    ("Notion", "Notion", False),
    ("SharePoint", "SharePoint", False),
)


def _connections_menu(root: str) -> str:
    """The Connections button and its menu: one switch per connected tool."""

    rows = "".join(
        f'<button type="button" class="conn-opt" role="menuitemcheckbox" aria-checked="{"true" if on else "false"}"'
        f' data-conn="{short}">{_brand(root, name)}<span>{name}</span><i class="toggle"><b></b></i></button>'
        for name, short, on in _CHAT_CONNECTIONS
    )
    label = ", ".join(short for _, short, on in _CHAT_CONNECTIONS if on)
    return (
        '<div class="picker">'
        f'<button type="button" class="pill" data-conn-btn aria-haspopup="menu">{_icon("link", "icon-sm")}'
        f"<span data-conn-label>Connections: {label}</span></button>"
        '<div class="model-menu conn-menu" data-conn-menu role="menu" hidden>'
        '<p class="menu-note">Tools this chat may use</p>'
        f"{rows}</div></div>"
    )


# OpenTeams' social profiles for the footer: name, address, and the icon's
# SVG body from Remix Icon 1.2.10 (@iconify-json/ri, Apache 2.0).
_SOCIAL = (
    (
        "LinkedIn",
        "https://www.linkedin.com/company/openteams",
        '<path fill="currentColor" d="M18.336 18.339h-2.665v-4.177c0-.996-.02-2.278-1.39-2.278c-1.389 0-1.601 1.084-1.601 2.205v4.25h-2.666V9.75h2.56v1.17h.035c.358-.674 1.228-1.387 2.528-1.387c2.7 0 3.2 1.778 3.2 4.092v4.714M7.004 8.575a1.546 1.546 0 0 1-1.548-1.549a1.548 1.548 0 1 1 1.547 1.549m1.336 9.764H5.667V9.75H8.34zM19.67 3H4.33C3.594 3 3 3.58 3 4.297v15.406C3 20.42 3.594 21 4.328 21h15.339C20.4 21 21 20.42 21 19.703V4.297C21 3.581 20.4 3 19.666 3z"/>',
    ),
    (
        "YouTube",
        "https://www.youtube.com/@openteams",
        '<path fill="currentColor" d="M12.244 4c.534.003 1.87.016 3.29.073l.504.022c1.429.067 2.857.183 3.566.38c.945.266 1.687 1.04 1.938 2.022c.4 1.56.45 4.602.456 5.339l.001.152v.174c-.007.737-.057 3.78-.457 5.339c-.254.985-.997 1.76-1.938 2.022c-.709.197-2.137.313-3.566.38l-.504.023c-1.42.056-2.756.07-3.29.072l-.235.001h-.255c-1.13-.007-5.856-.058-7.36-.476c-.944-.266-1.687-1.04-1.938-2.022c-.4-1.56-.45-4.602-.456-5.339v-.326c.006-.737.056-3.78.456-5.339c.254-.985.997-1.76 1.939-2.021c1.503-.419 6.23-.47 7.36-.476zM9.999 8.5v7l6-3.5z"/>',
    ),
    (
        "GitHub",
        "https://github.com/openteams-ai",
        '<path fill="currentColor" d="M12.001 2c-5.525 0-10 4.475-10 10a9.99 9.99 0 0 0 6.837 9.488c.5.087.688-.213.688-.476c0-.237-.013-1.024-.013-1.862c-2.512.463-3.162-.612-3.362-1.175c-.113-.288-.6-1.175-1.025-1.413c-.35-.187-.85-.65-.013-.662c.788-.013 1.35.725 1.538 1.025c.9 1.512 2.337 1.087 2.912.825c.088-.65.35-1.087.638-1.337c-2.225-.25-4.55-1.113-4.55-4.938c0-1.088.387-1.987 1.025-2.687c-.1-.25-.45-1.275.1-2.65c0 0 .837-.263 2.75 1.024a9.3 9.3 0 0 1 2.5-.337c.85 0 1.7.112 2.5.337c1.913-1.3 2.75-1.024 2.75-1.024c.55 1.375.2 2.4.1 2.65c.637.7 1.025 1.587 1.025 2.687c0 3.838-2.337 4.688-4.562 4.938c.362.312.675.912.675 1.85c0 1.337-.013 2.412-.013 2.75c0 .262.188.574.688.474A10.02 10.02 0 0 0 22 12c0-5.525-4.475-10-10-10"/>',
    ),
    (
        "X",
        "https://x.com/openteamsinc",
        '<path fill="currentColor" d="m17.687 3.063l-4.996 5.711l-4.32-5.711H2.112l7.477 9.776l-7.086 8.099h3.034l5.469-6.25l4.78 6.25h6.102l-7.794-10.304l6.625-7.571zm-1.064 16.06L5.654 4.782h1.803l10.846 14.34z"/>',
    ),
    (
        "Medium",
        "https://medium.com/openteams",
        '<path fill="currentColor" d="M6.158 3h11.684A3.16 3.16 0 0 1 21 6.158v3.366a4 4 0 0 0-.245-.008a2.91 2.91 0 0 0-2.294 1.1c-.472.586-.758 1.377-.828 2.266q-.022.266-.017.532c.047 2.014 1.135 3.624 3.068 3.624q.162 0 .316-.015v.82A3.16 3.16 0 0 1 17.842 21H6.158A3.16 3.16 0 0 1 3 17.842V6.158A3.16 3.16 0 0 1 6.158 3M21 15.388v-2.726h-1.653c-.074 1.316.646 2.43 1.653 2.726m0-3.107V9.973a1 1 0 0 0-.256-.035c-.815.017-1.3.992-1.36 2.343zM17.697 7.3l.015-.003v-.11h-2.9l-2.69 6.326L9.43 7.187H6.306v.11l.014.003c.529.12.797.298.797.94v7.52c0 .642-.27.82-.798.94l-.014.002v.11h2.12v-.11L8.41 16.7c-.529-.12-.798-.298-.798-.94V8.676l3.458 8.137h.196l3.559-8.364v7.496c-.046.508-.312.665-.791.773l-.014.003v.109h3.692v-.11l-.015-.002c-.48-.108-.752-.265-.797-.773l-.003-7.705h.003c0-.642.268-.82.797-.94"/>',
    ),
    (
        "Instagram",
        "https://www.instagram.com/openteams/",
        '<path fill="currentColor" d="M12.001 9a3 3 0 1 0 0 6a3 3 0 0 0 0-6m0-2a5 5 0 1 1 0 10a5 5 0 0 1 0-10m6.5-.25a1.25 1.25 0 0 1-2.5 0a1.25 1.25 0 0 1 2.5 0M12.001 4c-2.474 0-2.878.007-4.029.058c-.784.037-1.31.142-1.798.332a2.9 2.9 0 0 0-1.08.703a2.9 2.9 0 0 0-.704 1.08c-.19.49-.295 1.015-.331 1.798C4.007 9.075 4 9.461 4 12c0 2.475.007 2.878.058 4.029c.037.783.142 1.31.331 1.797c.17.435.37.748.702 1.08c.337.336.65.537 1.08.703c.494.191 1.02.297 1.8.333C9.075 19.994 9.461 20 12 20c2.475 0 2.878-.007 4.029-.058c.782-.037 1.308-.142 1.797-.331a2.9 2.9 0 0 0 1.08-.703c.337-.336.538-.649.704-1.08c.19-.492.296-1.018.332-1.8c.052-1.103.058-1.49.058-4.028c0-2.474-.007-2.878-.058-4.029c-.037-.782-.143-1.31-.332-1.798a2.9 2.9 0 0 0-.703-1.08a2.9 2.9 0 0 0-1.08-.704c-.49-.19-1.016-.295-1.798-.331C14.926 4.006 14.54 4 12 4m0-2c2.717 0 3.056.01 4.123.06c1.064.05 1.79.217 2.427.465c.66.254 1.216.598 1.772 1.153a4.9 4.9 0 0 1 1.153 1.772c.247.637.415 1.363.465 2.428c.047 1.066.06 1.405.06 4.122s-.01 3.056-.06 4.122s-.218 1.79-.465 2.428a4.9 4.9 0 0 1-1.153 1.772a4.9 4.9 0 0 1-1.772 1.153c-.637.247-1.363.415-2.427.465c-1.067.047-1.406.06-4.123.06s-3.056-.01-4.123-.06c-1.064-.05-1.789-.218-2.427-.465a4.9 4.9 0 0 1-1.772-1.153a4.9 4.9 0 0 1-1.153-1.772c-.248-.637-.415-1.363-.465-2.428C2.012 15.056 2 14.717 2 12s.01-3.056.06-4.122s.217-1.79.465-2.428a4.9 4.9 0 0 1 1.153-1.772A4.9 4.9 0 0 1 5.45 2.525c.637-.248 1.362-.415 2.427-.465C8.945 2.013 9.284 2 12.001 2"/>',
    ),
    (
        "Facebook",
        "https://www.facebook.com/openteamsinc/",
        '<path fill="currentColor" d="M12.001 2c-5.523 0-10 4.477-10 10c0 4.991 3.657 9.128 8.438 9.878v-6.987h-2.54V12h2.54V9.797c0-2.506 1.492-3.89 3.777-3.89c1.094 0 2.238.195 2.238.195v2.46h-1.26c-1.243 0-1.63.771-1.63 1.562V12h2.773l-.443 2.89h-2.33v6.988C18.344 21.129 22 16.992 22 12c0-5.523-4.477-10-10-10"/>',
    ),
)

# OpenTeams' own pages, as its website footer lists them.
_OPENTEAMS_LINKS = (
    ("About us", "https://openteams.com/about-us/"),
    ("Careers", "https://openteams.com/careers/"),
    ("Blog", "https://openteams.com/blog/"),
    ("Contact", "https://openteams.com/contact-us/"),
)


def _footer(root: str, domain: str, product: list[tuple[str, str]], hub: list[tuple[str, str]]) -> str:
    """The page footer: brand, three link columns, and the fine print."""

    def column(title: str, links) -> str:
        items = "".join(f'<li><a href="{html.escape(url)}">{html.escape(label)}</a></li>' for label, url in links)
        return f'<div class="foot-col"><h2>{title}</h2><ul>{items}</ul></div>'

    social = "".join(
        f'<a class="social" href="{url}" aria-label="{label}">'
        f'<svg viewBox="0 0 24 24" aria-hidden="true">{body}</svg></a>'
        for label, url, body in _SOCIAL
    )
    return (
        '<footer class="footer"><div class="foot-in">'
        '<div class="foot-brand">'
        f'<a class="foot-logo" href="{root}{HOME_PATH}"><img src="{root}{HOME_MARK_PATH}" alt="" width="28" height="28">'
        "Collab Hub</a>"
        "<p>Your team's AI workspace, running on your organization's own hub.</p>"
        f'<div class="socials">{social}</div></div>'
        f"{column('Product', product)}{column('Your hub', hub)}{column('OpenTeams', _OPENTEAMS_LINKS)}"
        "</div>"
        '<div class="foot-base">'
        f"<span>&copy; {datetime.now(UTC).year} OpenTeams. All rights reserved.</span>"
        f'<span class="foot-host">{html.escape(domain)}</span>'
        "</div></footer>"
    )


def _side_item(view: str, label: str, extra: str = "") -> str:
    return f'<label class="side-item{extra}" for="view-{view}">{_icon(_VIEW_ICONS[view], "icon-sm")}{label}</label>'


def _chat_replica(root: str) -> str:
    """Collab's chat screen, rebuilt in HTML so it can be used: pick a model,
    switch Frames, send a message. Replies are made up on the page by
    ``home.js``; nothing is sent anywhere."""

    frames = ("Q3 launch brief", "Brand voice")
    chips = "".join(
        f'<span class="fchip" data-frame="{name}">{name}<button type="button" aria-label="Remove {name}">'
        "&times;</button></span>"
        for name in frames
    )
    panel = "".join(
        f'<label class="pf"><input type="checkbox" data-frame-toggle="{name}"{" checked" if name in frames else ""}>'
        f"<span>{name}</span></label>"
        for name in ("Q3 launch brief", "Brand voice", "Customer research notes", "Support playbook", "Project naming")
    )
    model, logo = _DEFAULT_MODEL
    return (
        '<div class="view view-chat app" data-app>'
        '<aside class="app-side">'
        f'<div class="app-logo"><img src="{root}{HOME_MARK_PATH}" alt="" width="18" height="18">OpenTeams Collab</div>'
        f"{_side_item('hub', 'Intelligence Hub')}{_side_item('hub-usage', 'Hub Usage')}"
        "<hr>"
        f'{_side_item("frames", "Frames")}<span class="side-sub">Groups</span>'
        "<hr>"
        f"{_side_item('chat', 'Chat', ' active')}"
        f'<span class="side-sub">{_icon("history", "icon-sm")}History</span>'
        '<span class="side-hist" data-history>Launch update for Friday</span>'
        f'<span class="side-foot">{_icon("help", "icon-sm")}Help</span>'
        f'<span class="side-foot">{_icon("settings", "icon-sm")}Settings</span>'
        "</aside>"
        '<section class="app-main">'
        '<header class="app-head"><span class="hub-badge">Hub</span>'
        f'<span class="head-model" data-model-name>{model}</span>'
        '<span class="head-title" data-title>New chat</span>'
        '<span class="head-actions"><span class="ghost-btn">Export</span><span class="ghost-btn">History</span>'
        '<button type="button" class="new-btn" data-new>New Chat</button></span></header>'
        '<div class="app-msgs" data-msgs><div class="empty" data-empty>'
        f'<img src="{root}{HOME_MARK_PATH}" alt="" width="40" height="40">'
        "<p>Ask anything. Your Frames and connected tools are ready.</p>"
        '<div class="starters">'
        '<button type="button" data-starter>Create a report for Q4</button>'
        '<button type="button" data-starter>Which customers are at risk of churning?</button>'
        '<button type="button" data-starter>Search my Drive for healthcare past performance</button>'
        "</div></div></div>"
        '<form class="composer" data-form>'
        f'<div class="frame-bar"><span class="fb-label" data-frame-count>Frames · {len(frames)}</span>{chips}</div>'
        '<textarea data-input rows="2" placeholder="Send a message..." aria-label="Message"></textarea>'
        '<div class="composer-row"><div class="picker">'
        f'<button type="button" class="model-btn" data-model-btn aria-haspopup="menu">{_logo(root, logo)}'
        f"<span data-model-label>{model}</span>"
        '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>'
        f"{_model_menu(root)}</div>"
        f"{_connections_menu(root)}"
        f'<button type="submit" class="send-btn" aria-label="Send">{_icon("up", "icon-sm")}</button>'
        "</div></form></section>"
        '<aside class="app-frames"><p class="pf-title">Frames</p><p class="pf-sub">Selected Frames apply to this chat</p>'
        f"{panel}</aside>"
        "</div>"
    )


def _showcase(root: str, domain: str) -> str:
    """Collab, clickable: the tabs and the screenshot's own sidebar switch views.

    No script: one radio per view, and labels for it in the tab row and over
    the screenshot's sidebar entry. The stylesheet shows the checked view.
    """

    radios = "".join(
        f'<input class="view-radio" type="radio" name="view" id="view-{view}"'
        f'{" checked" if i == 0 else ""} aria-label="{html.escape(label)}">'
        for i, (view, label, _) in enumerate(_VIEWS)
    )
    tabs = "".join(
        f'<label class="tab tab-{view}" for="view-{view}">{_icon(_VIEW_ICONS[view], "icon-sm")}<span>'
        + label.replace("Intelligence ", '<span class="wide">Intelligence </span>').replace(
            "Hub Usage", '<span class="wide">Hub </span>Usage'
        )
        + "</span></label>"
        for view, label, _ in _VIEWS
    )
    shots = _chat_replica(root) + "".join(
        _shot(root, view, f"Collab: {label}", f"shot view view-{view}") for view, label, _ in _VIEWS if view != "chat"
    )
    hotspots = "".join(
        f'<label class="hot hot-{view}" for="view-{view}"><span class="hot-tip">{label}</span></label>'
        for view, label, _ in _VIEWS
    )
    return (
        f'<div class="showcase">{radios}'
        f'<div class="tabs">{tabs}</div>'
        '<div class="window">'
        f'<div class="window-bar"><span></span><span></span><span></span><em>collab.{html.escape(domain)}</em></div>'
        f'<div class="screen">{shots}{hotspots}</div>'
        "</div>"
        "</div>"
    )


def _art_frames() -> str:
    """Three Frames; two switch on, and a new chat starts with exactly those two."""

    def card(name: str, on: bool, step: int) -> str:
        state = f" on s{step}" if on else ""
        return (
            f'<div class="fcard{state}">{_icon("frames", "icon-sm")}<span>{name}</span>'
            '<i class="toggle"><b></b></i></div>'
        )

    return (
        '<div class="art art-frames" aria-hidden="true">'
        '<div class="fcards">'
        f"{card('Q3 launch brief', True, 1)}{card('Brand voice', True, 2)}{card('Support playbook', False, 0)}"
        "</div>"
        '<div class="fflow"><i class="fline"></i><i class="fdot"></i></div>'
        '<div class="newchat"><div class="nc-head">New chat<span>2 Frames</span></div>'
        '<div class="nc-chips"><span class="chip c1">Q3 launch brief</span><span class="chip c2">Brand voice</span></div>'
        '<div class="nc-input">Draft the launch update<i class="send"></i></div></div>'
        "</div>"
    )


# Node centres of the connector diagram, in % of its stage. The SVG spokes
# use the same numbers on a 100 x 100 grid, so every line meets its node.
_TOOL_NODES = (("Gmail", 17, 20), ("Google Drive", 83, 20), ("Google Calendar", 17, 80), ("GitHub", 83, 80))
_CORE_NODE = (50, 50)


def _art_connectors(root: str) -> str:
    """Collab in the middle of your tools; one question goes out to Drive and comes back."""

    cx, cy = _CORE_NODE
    spokes = "".join(
        f'<line{' class="live"' if name == "Google Drive" else ""} x1="{cx}" y1="{cy}" x2="{x}" y2="{y}"/>'
        for name, x, y in _TOOL_NODES
    )
    tools = "".join(
        f'<div class="tool t{i}{" live" if name == "Google Drive" else ""}">{_brand(root, name)}<span>{name}</span></div>'
        for i, (name, _, _) in enumerate(_TOOL_NODES)
    )
    return (
        '<div class="art art-tools" aria-hidden="true"><div class="stage">'
        f'<svg class="spokes" viewBox="0 0 100 100" preserveAspectRatio="none">{spokes}</svg>'
        f"{tools}"
        '<i class="ping"></i>'
        f'<div class="core"><img src="{root}{HOME_MARK_PATH}" alt="" width="40" height="40"></div>'
        "</div>"
        '<div class="call"><span class="call-name"><code>search_drive</code>query: "launch update"</span>'
        '<span class="ok">3 files found</span></div>'
        "</div>"
    )


def _art_hub(domain: str) -> str:
    """Everything sits inside your hub's boundary; usage is counted per person."""

    tiles = "".join(
        f'<div class="htile h{i}">{_icon(icon)}<span>{label}</span></div>'
        for i, (icon, label) in enumerate((("spark", "Models"), ("frames", "Frames"), ("shield", "Connector tokens")))
    )
    bars = "".join(
        f'<div class="ubar"><span>{who}</span><i class="b{i}"></i></div>' for i, who in enumerate(("You", "Team"))
    )
    return (
        '<div class="art art-hub" aria-hidden="true">'
        f'<div class="boundary"><span class="blabel">{html.escape(domain)}</span>'
        f'<div class="htiles">{tiles}</div>'
        f'<div class="usage"><span class="ulabel">Usage this month</span>{bars}</div></div>'
        "</div>"
    )


def _chapter(title: str, text: str, points: tuple[str, ...], art: str) -> str:
    items = "".join(f"<li>{html.escape(point)}</li>" for point in points)
    return (
        '<section class="band chapter">'
        f'<div class="chapter-copy">'
        f"<h2>{html.escape(title)}</h2><p>{html.escape(text)}</p><ul>{items}</ul></div>"
        f'<div class="chapter-art">{art}</div>'
        "</section>"
    )


@dataclass(frozen=True)
class _Chrome:
    """What every hub page shares: its links, header actions and footer columns."""

    root: str
    domain: str
    web_client: str
    frames: str
    download: str
    nav: str
    header_actions: str
    product_links: list[tuple[str, str]]
    hub_links: list[tuple[str, str]]
    sign_in: str
    sign_up: str


def _chrome(surface: WebSurface, session: WebSession | None, root_path: str, back_path: str) -> _Chrome:
    domain = hub_domain(surface)
    web_client = f"https://collab.{domain}/"
    # Frames are managed in the web client; the hub's own host serves its API.
    frames = f"https://collab.{domain}/frames"
    api_docs = f"https://frames.{domain}/docs"
    root = html.escape(root_path)
    download = surface.desktop_download_url
    docs = surface.docs_url

    nav = [f'<a href="{html.escape(web_client)}">Collab</a>', f'<a href="{html.escape(frames)}">Frames</a>']
    if docs:
        nav.append(f'<a href="{html.escape(docs)}">Docs</a>')
    if download:
        nav.append(f'<a href="{html.escape(download)}">Download</a>')

    product_links = [("Open Collab", web_client)]
    if download:
        product_links.append(("Download for macOS", download))
    product_links.append(("Frames", frames))
    if docs:
        product_links.append(("Docs", docs))
    product_links.append(("API docs", api_docs))
    legal_links = [
        ("Your data", f"{root_path}{DATA_STATEMENT_PATH}"),
        ("Terms", f"{root_path}{TERMS_PATH}"),
        ("Privacy", f"{root_path}{PRIVACY_PATH}"),
    ]

    back = quote(back_path, safe="")
    sign_in = f"{root_path}{SIGNIN_PATH}?next={back}"
    sign_up = f"{root_path}{SIGNIN_PATH}?register=1&next={back}"
    toggle = (
        '<button type="button" class="theme-toggle" data-theme-toggle aria-label="Switch light or dark theme">'
        f"{_icon('sun', 'icon-sm in-dark-inline')}{_icon('moon', 'icon-sm in-light-inline')}</button>"
    )
    if session is None:
        header_actions = (
            f'{toggle}<a class="btn quiet" href="{html.escape(sign_in)}">Log in</a>'
            f'<a class="btn primary" href="{html.escape(sign_up)}">Sign up</a>'
        )
        hub_links = [("Log in", sign_in), ("Create an account", sign_up), *legal_links]
    else:
        identity = session.name or session.email or session.user
        account = f"{root_path}{ACCOUNT_PATH}"
        email = session.email if session.email and session.email != identity else ""
        header_actions = (
            f"{toggle}"
            '<details class="menu"><summary class="who" aria-label="Your account">'
            f'{_avatar(session.user)}<span class="who-name">{html.escape(identity)}</span>'
            '<svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></summary>'
            '<div class="menu-pop">'
            f'<div class="menu-head">{_avatar(session.user)}<div><strong>{html.escape(identity)}</strong>'
            f"{f'<span>{html.escape(email)}</span>' if email else ''}</div></div>"
            f'<a class="menu-item" href="{html.escape(account)}">{_icon("account", "icon-sm")}Account</a>'
            f"{_signout_form(root, session, 'menu-item')}"
            "</div></details>"
        )
        hub_links = [("Your account", account), *legal_links]
    return _Chrome(
        root=root,
        domain=domain,
        web_client=web_client,
        frames=frames,
        download=download,
        nav="".join(nav),
        header_actions=header_actions,
        product_links=product_links,
        hub_links=hub_links,
        sign_in=sign_in,
        sign_up=sign_up,
    )


def _signout_form(root: str, session: WebSession, css_class: str) -> str:
    return (
        f'<form method="post" action="{root}{SIGNOUT_PATH}">'
        f'<input type="hidden" name="csrf_token" value="{html.escape(session.csrf)}">'
        f'<button type="submit" class="{css_class}">{_icon("logout", "icon-sm")}Log out</button></form>'
    )


def _document(
    *,
    title: str,
    chrome: _Chrome,
    main: str,
    scripts: tuple[str, ...] = (),
    footer: bool = False,
    framed: bool = True,
) -> str:
    """A complete hub page: head, header, ``main``, footer. ``framed`` puts
    ``main`` in the landing page's centred column with its side rules and
    its section links in the header; unframed, a dashboard fills the window
    beside its own sidebar, with the logo at the far left above it."""

    root = chrome.root
    tail = "".join(f'<script src="{root}{path}" defer></script>\n' for path in scripts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)}</title>
<link rel="icon" href="{root}{HOME_MARK_PATH}" type="image/svg+xml">
<link rel="preload" href="{root}{HOME_FONT_PATHS[0]}" as="font" type="font/woff2" crossorigin>
<link rel="preload" href="{root}{HOME_FONT_PATHS[3]}" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{root}{HOME_STYLE_PATH}">
<script src="{root}{HOME_THEME_SCRIPT_PATH}"></script>
</head>
<body>
<header class="topbar"><div class="topbar-in{"" if framed else " wide"}">
<a class="brand" href="{root}{HOME_PATH}">
<img src="{root}{HOME_MARK_PATH}" alt="" width="28" height="28"><span>Collab Hub</span></a>
<nav class="nav">{chrome.nav if framed else ""}</nav>
<div class="actions">{chrome.header_actions}</div>
</div></header>
<div class="{"frame" if framed else "shell"}">
{main}
</div>
{_footer(root, chrome.domain, chrome.product_links, chrome.hub_links) if footer else ""}
{tail}</body>
</html>
"""


# A pencil-drawn squiggle under the hero's "knows": one stroke, and a fainter
# second pass beside it for the grain of a pencil line. Decoration only.
_SCRIBBLE = (
    '<svg class="scribble-line" viewBox="0 0 220 24" preserveAspectRatio="none" aria-hidden="true">'
    '<path d="M4 15c18-7 33 5 52-1s31-9 50-2 33 6 52-1 31-6 56-2"/>'
    '<path class="grain" d="M8 18c20-5 36 3 54-2s33-7 51-1 31 4 50-1 30-4 50 0"/>'
    "</svg>"
)


def home_page(*, surface: WebSurface, session: WebSession | None, root_path: str = "") -> str:
    chrome = _chrome(surface, session, root_path, HOME_PATH)
    root, domain, web_client, download = chrome.root, chrome.domain, chrome.web_client, chrome.download
    docs = surface.docs_url

    doors = [_door("collab", "Open Collab in your browser", "Nothing to install.", web_client)]
    if download:
        doors.append(_door("apple", "Download Collab for macOS", "Local agents and capabilities.", download))
    doors.append(_door("frames", "Manage your Frames", "Your team's shared context.", chrome.frames))
    if docs:
        doors.append(_door("docs", "Read the docs", "Set up, connect tools, go further.", docs))

    if session is None:
        sign_in, sign_up = html.escape(chrome.sign_in), html.escape(chrome.sign_up)
        hero_actions = (
            f'<a class="btn primary large" href="{sign_up}">Get started{_icon("arrow", "icon-sm")}</a>'
            f'<a class="btn outline large" href="{sign_in}">Log in</a>'
        )
        closing_title = "Bring your team in."
        closing_text = "Create an account on this hub and start your first chat in a minute."
        closing_action = f'<a class="btn primary large" href="{sign_up}">Create your account</a>'
    else:
        hero_actions = f'<a class="btn primary large" href="{html.escape(web_client)}">{_mark()}Open Collab</a>'
        if download:
            hero_actions += (
                f'<a class="btn outline large" href="{html.escape(download)}">{_apple()}Download for macOS</a>'
            )
        closing_title = "Pick up where you left off."
        closing_text = "Your chats, Frames and connectors are waiting in Collab."
        closing_action = f'<a class="btn primary large" href="{html.escape(web_client)}">{_mark()}Open Collab</a>'

    chapters = "".join(
        (
            _chapter(
                "Write it down once.",
                "A Frame is a short Markdown note: the launch brief, your brand voice, the support playbook."
                " Switch it on, and every new chat starts with it.",
                ("Shared with your team, or kept private", "Tagged, searchable, switched on per chat"),
                _art_frames(),
            ),
            _chapter(
                "Ask across your tools.",
                "Collab reaches into Drive, Gmail, Calendar and GitHub for you, and shows every call it made,"
                " with the arguments and what came back.",
                ("Connect each tool once, on the hub", "Every tool call is visible in the chat"),
                _art_connectors(root),
            ),
            _chapter(
                "On your hub, under your rules.",
                "Models, Frames and connector tokens live on your organization's hub."
                " You see your own usage; your admins see the whole hub.",
                ("Models served by your hub's gateway", "Usage you can see, per person"),
                _art_hub(domain),
            ),
        )
    )

    main = f"""<section class="band hero">
<h1>AI that already <span class="scribble">knows{_SCRIBBLE}</span><br>how your <span class="scribble">team works{_SCRIBBLE}</span>.</h1>
<p class="lead">Collab is your team's AI workspace. Write your context down once, connect the tools you use,
and every chat starts informed. It all runs on your organization's own hub.</p>
<div class="hero-actions">{hero_actions}</div>
</section>
<section class="band show">{_showcase(root, domain)}</section>
<section class="band connectors">
<p>Works with the tools your team already uses</p>
<ul>{_connectors(root)}</ul>
</section>
<section class="band problem">
<h2>Most AI chats know nothing about your team.</h2>
<div class="pains">
<div><h3>{_icon("repeat")}They forget your work</h3>
<p>Each new chat needs the project, the audience and the tone pasted in by hand.</p></div>
<div><h3>{_icon("files")}They can't see your tools</h3>
<p>The answer sits in Drive, Gmail or tomorrow's meeting, and you copy it across.</p></div>
<div><h3>{_icon("exit")}They run somewhere else</h3>
<p>What your team knows ends up in services nobody in your company runs.</p></div>
</div>
</section>
{chapters}
<section class="band ways">
<h2>Start wherever you work.</h2>
<div class="doors">{"".join(doors)}</div>
</section>
<section class="band closing">
<h2>{html.escape(closing_title)}</h2>
<p>{html.escape(closing_text)}</p>
{closing_action}
</section>"""
    return _document(title="Collab Hub", chrome=chrome, main=main, scripts=(HOME_SCRIPT_PATH,), footer=True)


# The account dashboard's sections: path, label, icon, in sidebar order.
_DASHBOARD = (
    (ACCOUNT_PATH, "Account", "account"),
    (BILLING_PATH, "Billing", "card"),
)


def _dashboard(*, surface: WebSurface, session: WebSession, root_path: str, current: str, title: str, body: str) -> str:
    """The account dashboard: a sidebar of sections beside the one shown."""

    chrome = _chrome(surface, session, root_path, current)

    def entry(href: str, label: str, icon: str, attrs: str = "") -> str:
        # The name doubles as a tooltip, for when the sidebar is folded to icons.
        return (
            f'<li><a class="dash-link" href="{html.escape(href)}"{attrs} title="{label}">'
            f"{icon}<span>{label}</span></a></li>"
        )

    sections = "".join(
        entry(root_path + path, label, _icon(icon, "icon-sm"), ' aria-current="page"' if path == current else "")
        for path, label, icon in _DASHBOARD
    )
    product = entry(chrome.web_client, "Collab", _mark()) + entry(chrome.frames, "Frames", _icon("frames", "icon-sm"))
    fold = (
        '<button type="button" class="dash-fold" data-sidebar-toggle aria-controls="dash-nav" '
        f'aria-expanded="true" aria-label="Collapse sidebar">{_icon("sidebar", "icon-sm")}</button>'
    )
    main = (
        '<section class="dash">'
        f'<nav class="dash-nav" id="dash-nav" aria-label="Account"><ul>{sections}</ul><ul>{product}</ul>{fold}</nav>'
        f'<div class="dash-main">{body}</div>'
        "</section>"
    )
    return _document(title=f"{title} · Collab Hub", chrome=chrome, main=main, framed=False)


def account_page(*, surface: WebSurface, session: WebSession, root_path: str = "") -> str:
    """The signed-in person's account, in the home page's look and theme."""

    domain = hub_domain(surface)
    identity = session.name or session.email or session.user
    rows = [("Name", html.escape(session.name or identity))]
    if session.email:
        rows.append(("Email", html.escape(session.email)))
    rows.append(("Hub", f'<span class="mono">{html.escape(domain)}</span>'))
    details = "".join(f"<div><dt>{label}</dt><dd>{value}</dd></div>" for label, value in rows)
    body = f"""<div class="acct-head">{_avatar(session.user)}<div><h1>{html.escape(identity)}</h1>
<p>Your account on {html.escape(domain)}</p></div></div>
<dl class="acct-list">{details}</dl>
<div class="acct-actions">
<a class="btn primary" href="{html.escape(f"https://collab.{domain}/")}">{_mark()}Open Collab</a>
{_signout_form(html.escape(root_path), session, "btn outline")}
</div>"""
    return _dashboard(
        surface=surface, session=session, root_path=root_path, current=ACCOUNT_PATH, title="Your account", body=body
    )


def billing_page(*, surface: WebSurface, session: WebSession, root_path: str = "") -> str:
    """Billing, in the account dashboard. Empty for now."""

    plan = (
        f'<a class="btn outline" href="{html.escape(surface.account_page_url)}">{_icon("card", "icon-sm")}'
        "Plan and billing</a>"
        if surface.account_page_url
        else ""
    )
    body = f"<h1>Billing</h1>{plan}"
    return _dashboard(
        surface=surface, session=session, root_path=root_path, current=BILLING_PATH, title="Billing", body=body
    )


# The Collab mark's four quadrants: shape and colour. The served mark draws
# them in their blues; on a solid blue button they are drawn in one colour.
_MARK_QUADRANTS = (
    ("M40.12 5.333A26.803 26.803 0 0 0 10.613 32H26.853A13.267 13.267 0 0 1 40.12 18.733Z", "#2952e3"),
    ("M40.12 18.733A13.267 13.267 0 0 1 53.387 32H40.12Z", "#7d9bff"),
    ("M53.387 32A13.267 13.267 0 0 1 40.12 45.267V32Z", "#4d75fe"),
    ("M10.613 32A26.803 26.803 0 0 0 40.12 58.667V45.267A13.267 13.267 0 0 1 26.853 32Z", "#1a3bb8"),
)

COLLAB_MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" fill="none">\n'
    + "".join(f'<path fill="{colour}" d="{d}"/>\n' for d, colour in _MARK_QUADRANTS)
    + "</svg>\n"
)
"""The Collab mark's shape, in four blues so it reads on black and on white."""


_FONT_FACES = "\n".join(
    f"@font-face {{ font-family: '{family}'; font-style: normal; font-weight: {weight};"
    f" font-display: swap; src: url({path}) format('woff2'); }}"
    for path, family, weight in zip(
        HOME_FONT_PATHS,
        ("IBM Plex Sans",) * 4 + ("Cascadia Code", "IBM Plex Mono"),
        (400, 500, 600, 700, 400, 400),
        strict=True,
    )
)


HOME_STYLESHEET = (
    _FONT_FACES
    + """
:root {
  color-scheme: light;
  --font-sans: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: 'Cascadia Code', ui-monospace, Menlo, monospace;
  --blue: #2952e3;
  --blue-hover: #1f44cc;
  --bg: #ffffff;
  --panel: #f7f7f8;
  --line: #e4e4e7;
  --text: #09090b;
  --muted: #52525b;
  --faint: #71717a;
  --accent: #2952e3;
  --tile: #eef2ff;
  --chrome: #f4f4f5;
}
:root[data-theme="dark"] {
  color-scheme: dark;
    --bg: #050506;
    --panel: #0c0c0e;
    --line: #1f1f24;
    --text: #f4f4f5;
    --muted: #a1a1aa;
    --faint: #8e8e99;
    --accent: #7d9bff;
    --tile: #10183a;
    --chrome: #111114;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  display: flex; flex-direction: column; min-height: 100vh;
  margin: 0; background: var(--bg); color: var(--text);
  font-family: var(--font-sans); font-size: 16px; line-height: 1.55; -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-underline-offset: 3px; }
::selection { background: var(--blue); color: #ffffff; }
html { scrollbar-color: var(--line) transparent; accent-color: var(--blue); caret-color: var(--accent); }
.icon { width: 20px; height: 20px; flex: none; }
.icon-sm { width: 16px; height: 16px; flex: none; }

/* Top bar */
.topbar {
  position: sticky; top: 0; z-index: 10; background: var(--bg); border-bottom: 1px solid var(--line);
}
.topbar-in { display: flex; align-items: center; gap: 32px; max-width: 1240px; margin: 0 auto; padding: 14px 32px; }
.brand { display: inline-flex; align-items: center; gap: 10px; text-decoration: none; font-weight: 600; font-size: 17px; }
.nav { display: flex; gap: 26px; margin-right: auto; }
.nav a { text-decoration: none; color: var(--muted); font-size: 15px; padding: 4px 0; }
.nav a:hover { color: var(--text); }
.actions { display: flex; align-items: center; gap: 8px; }
.actions form { margin: 0; }
.theme-toggle {
  display: inline-grid; place-items: center; width: 36px; height: 36px; border-radius: 8px;
  border: 1px solid var(--line); background: transparent; color: var(--muted); cursor: pointer;
}
.theme-toggle:hover { color: var(--text); border-color: var(--faint); }
.in-dark-inline { display: none; }
:root[data-theme="dark"] .in-dark-inline { display: block; }
:root[data-theme="dark"] .in-light-inline { display: none; }
.menu { position: relative; }
.who {
  display: inline-flex; align-items: center; gap: 10px; padding: 4px 8px 4px 4px; border-radius: 999px;
  border: 1px solid var(--line); font-size: 14px; cursor: pointer; list-style: none; user-select: none;
}
.who::-webkit-details-marker { display: none; }
.who:hover, .menu[open] .who { border-color: var(--faint); }
.avatar { display: block; width: 28px; height: 28px; border-radius: 50%; flex: none; }
.chev { width: 14px; height: 14px; fill: none; stroke: var(--faint); stroke-width: 2; }
.menu-pop {
  position: absolute; right: 0; top: calc(100% + 8px); width: 260px; padding: 6px;
  background: var(--bg); border: 1px solid var(--line); border-radius: 12px;
  box-shadow: 0 12px 32px rgb(0 0 0 / 18%);
}
.menu-pop form { margin: 0; }
.menu-head { display: flex; align-items: center; gap: 12px; padding: 10px 10px 12px; border-bottom: 1px solid var(--line); margin-bottom: 6px; }
.menu-head .avatar { width: 36px; height: 36px; }
.menu-head strong { display: block; font-size: 14px; font-weight: 600; }
.menu-head span { display: block; font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
.menu-item {
  display: flex; align-items: center; gap: 10px; width: 100%; padding: 9px 10px; border: 0; border-radius: 8px;
  background: transparent; color: var(--text); font: inherit; font-size: 14px; font-weight: 500; text-align: left; text-decoration: none; cursor: pointer;
}
.menu-item:hover { background: var(--panel); }
.menu-item .icon-sm { color: var(--text); }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  font: inherit; font-size: 15px; font-weight: 500; line-height: 22px;
  padding: 7px 16px; border-radius: 8px; border: 1px solid transparent;
  text-decoration: none; cursor: pointer; white-space: nowrap;
  transition: background-color 120ms ease, border-color 120ms ease;
}
.btn.large { font-size: 16px; padding: 12px 22px; }
.btn.primary { background: var(--blue); color: #ffffff; }
.btn.primary:hover { background: var(--blue-hover); }
.btn.outline { background: transparent; color: var(--text); border-color: var(--line); }
.btn.outline:hover { border-color: var(--faint); }
.btn.quiet { background: transparent; color: var(--text); }
.btn.quiet:hover { background: var(--panel); }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }

/* The frame: one column with hairline sides, sections stacked with hairlines between. */
.frame { flex: 1; width: 100%; max-width: 1240px; margin: 0 auto; border-left: 1px solid var(--line); border-right: 1px solid var(--line); }
.band { padding: 72px 64px; border-bottom: 1px solid var(--line); }
h1 {
  text-wrap: balance; margin: 0 0 24px; font-size: clamp(40px, 6vw, 72px); line-height: 1.02;
  font-weight: 600; letter-spacing: -0.03em;
}
h2 { text-wrap: balance; margin: 0 0 16px; font-size: clamp(28px, 3.4vw, 40px); line-height: 1.1; font-weight: 600; letter-spacing: -0.02em; }
h3 { margin: 0 0 6px; font-size: 17px; font-weight: 600; }

.hero { padding-top: 96px; padding-bottom: 56px; text-align: center; }
.host-sep { width: 1px; height: 12px; background: var(--line); }
.host {
  display: inline-flex; align-items: center; gap: 10px; margin: 0 0 28px; padding: 6px 14px;
  border: 1px solid var(--line); border-radius: 999px; font-family: var(--font-mono); font-size: 13px; color: var(--muted);
}
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--blue); }
.scribble { position: relative; display: inline-block; white-space: nowrap; }
.scribble-line {
  position: absolute; left: -2%; bottom: -0.16em; width: 104%; height: 0.32em; overflow: visible; pointer-events: none;
}
.scribble-line path {
  fill: none; stroke: var(--accent); stroke-width: 3.2px; stroke-linecap: round; stroke-linejoin: round;
  vector-effect: non-scaling-stroke; stroke-dasharray: 2000; stroke-dashoffset: 0;
  animation: scribble 900ms 450ms cubic-bezier(.4, 0, .2, 1) both;
}
.scribble-line .grain { stroke-width: 1.4px; opacity: 0.45; animation-delay: 650ms; }
/* Non-scaling strokes dash in screen pixels: 2000 outlasts any underline. */
@keyframes scribble { from { stroke-dashoffset: 2000; } }
.lead { text-wrap: pretty; margin: 0 auto 36px; max-width: 34em; font-size: 19px; line-height: 1.6; color: var(--muted); }
.hero-actions { display: flex; flex-wrap: wrap; justify-content: center; gap: 12px; }

/* The clickable Collab window. */
.show { padding-top: 48px; background: var(--panel); }
.view-radio { position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }
.tabs {
  display: flex; width: max-content; max-width: 100%; margin: 0 auto 24px; padding: 4px; gap: 2px;
  border: 1px solid var(--line); border-radius: 10px; background: var(--bg); overflow-x: auto;
}
.tab {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px; padding: 6px 14px; border-radius: 7px; font-size: 14px; color: var(--muted); cursor: pointer;
  user-select: none; white-space: nowrap; transition: background-color 120ms ease, color 120ms ease;
}
.tab:hover { color: var(--text); }
.window {
  border: 1px solid var(--line); border-radius: 12px; overflow: hidden; background: var(--chrome);
}
.window-bar { display: flex; align-items: center; gap: 7px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
.window-bar span { width: 12px; height: 12px; border-radius: 50%; box-shadow: inset 0 0 0 0.5px rgb(0 0 0 / 22%); }
/* macOS window controls, in their real colours: the one place the page
   uses colour outside blue and black. */
.window-bar span:nth-child(1) { background: #ff5f57; }
.window-bar span:nth-child(2) { background: #febc2e; }
.window-bar span:nth-child(3) { background: #28c840; }
.window-bar em { margin-left: 8px; font-style: normal; font-family: var(--font-mono); font-size: 12px; color: var(--faint); }
.screen { position: relative; }
.shot img { display: block; width: 100%; height: auto; }
.in-dark { display: none !important; }
:root[data-theme="dark"] .in-dark { display: block !important; }
:root[data-theme="dark"] .in-light { display: none !important; }
.view { display: none; }
.hot { position: absolute; border-radius: 6px; cursor: pointer; transition: background-color 120ms ease; }
/* Hover tints the entry the way Collab's own sidebar does; the open view's
   entry is already drawn selected in its screenshot, so it gets no tint. */
.hot:hover { background: rgb(255 255 255 / 6%); }
:root[data-theme="light"] .hot:hover { background: rgb(0 0 0 / 5%); }
#view-frames:checked ~ .window .hot-frames:hover,
#view-hub:checked ~ .window .hot-hub:hover,
#view-hub-usage:checked ~ .window .hot-hub-usage:hover { background: none; }
.hot-tip { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
#view-chat:checked ~ .window .view-chat,
#view-frames:checked ~ .window .view-frames,
#view-hub:checked ~ .window .view-hub,
#view-hub-usage:checked ~ .window .view-hub-usage { display: block; }
#view-chat:checked ~ .tabs .tab-chat,
#view-frames:checked ~ .tabs .tab-frames,
#view-hub:checked ~ .tabs .tab-hub,
#view-hub-usage:checked ~ .tabs .tab-hub-usage { background: var(--blue); color: #ffffff; }
#view-chat:focus-visible ~ .tabs .tab-chat,
#view-frames:focus-visible ~ .tabs .tab-frames,
#view-hub:focus-visible ~ .tabs .tab-hub,
#view-hub-usage:focus-visible ~ .tabs .tab-hub-usage { outline: 2px solid var(--accent); outline-offset: 2px; }
/* The chat replica: Collab's own chat screen, in its own colours. */
.app {
  --app-bg: #ffffff; --app-side: #fafafa; --app-line: #e4e4e7; --app-text: #09090b; --app-muted: #71717a;
  --app-soft: #f4f4f5; --app-active: #2952e3;
  aspect-ratio: 16 / 10; grid-template-columns: 18% minmax(0, 1fr) 24%;
  background: var(--app-bg); color: var(--app-text); font-size: 13px; text-align: left;
}
:root[data-theme="dark"] .app {
    --app-bg: #0a0a0a; --app-side: #111111; --app-line: #262626; --app-text: #f4f4f5; --app-muted: #a1a1aa;
    --app-soft: #1c1c1c;
}
#view-chat:checked ~ .window .view-chat { display: grid; }
#view-chat:checked ~ .window .hot { display: none; }
.app-side {
  display: flex; flex-direction: column; gap: 2px; padding: 12px 8px; min-height: 0;
  background: var(--app-side); border-right: 1px solid var(--app-line);
}
.app-logo { display: flex; align-items: center; gap: 8px; padding: 2px 8px 12px; font-weight: 600; font-size: 14px; }
.app-side hr { width: 100%; border: 0; border-top: 1px solid var(--app-line); margin: 6px 0; }
.side-item, .side-sub, .side-foot {
  display: flex; align-items: center; gap: 8px; padding: 7px 8px; border-radius: 7px; cursor: pointer;
}
.side-item:hover { background: var(--app-soft); }
.side-item.active { background: var(--app-active); color: #ffffff; }
.side-sub { padding-left: 22px; color: var(--app-muted); cursor: default; }
.side-hist {
  margin-left: 22px; padding: 6px 8px; border-left: 1px solid var(--app-line); color: var(--app-muted);
  font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.side-foot { color: var(--app-muted); cursor: default; }
.side-foot:first-of-type { margin-top: auto; }
.app-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.app-head {
  display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-bottom: 1px solid var(--app-line);
}
.hub-badge { padding: 2px 8px; border-radius: 999px; background: var(--app-active); color: #ffffff; font-size: 11px; font-weight: 600; }
.head-model { font-weight: 600; white-space: nowrap; }
.head-title { min-width: 0; padding-left: 10px; border-left: 1px solid var(--app-line); color: var(--app-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.head-actions { display: flex; gap: 6px; margin-left: auto; }
.ghost-btn, .new-btn {
  padding: 5px 10px; border-radius: 7px; border: 1px solid var(--app-line); font: inherit; font-size: 12px;
  background: transparent; color: var(--app-text); white-space: nowrap;
}
.new-btn { background: var(--app-text); color: var(--app-bg); border-color: var(--app-text); cursor: pointer; }
.app-msgs { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding: 18px 20px; }
.empty { margin: auto; display: grid; justify-items: center; gap: 12px; text-align: center; color: var(--app-muted); }
.empty p { margin: 0; }
.starters { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; }
.starters button {
  padding: 7px 12px; border-radius: 999px; border: 1px solid var(--app-line); background: var(--app-bg);
  color: var(--app-text); font: inherit; font-size: 12px; cursor: pointer;
}
.starters button:hover { border-color: var(--app-active); }
.msg { max-width: 80%; line-height: 1.55; white-space: pre-wrap; }
.msg.me {
  align-self: flex-end; padding: 9px 14px; border-radius: 10px; background: var(--app-soft);
  border-left: 3px solid var(--app-active);
}
.msg.ai { align-self: flex-start; }
.msg-meta { margin-bottom: 4px; font-family: var(--font-mono); font-size: 11px; color: var(--app-muted); }
.thinking { color: var(--app-muted); }
.thinking::after { content: "..."; }
.toolcall { align-self: flex-start; border: 1px solid var(--app-line); border-radius: 8px; padding: 6px 10px; font-size: 12px; }
.toolcall summary { cursor: pointer; font-weight: 600; }
.toolbody { display: grid; gap: 2px; margin-top: 6px; font-family: var(--font-mono); font-size: 11px; color: var(--app-muted); }
.toolbody code { color: var(--app-active); font-family: inherit; }
.msg-after { margin-top: 12px; }
/* A chart drawn into a reply: its bars grow in one after another. */
.chart {
  margin: 12px 0 0; width: min(420px, 100%); padding: 12px 14px 8px; border: 1px solid var(--app-line);
  border-radius: 10px; background: var(--app-bg); white-space: normal; animation: rise 0.4s ease both;
}
.chart-head { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; font-weight: 600; }
.chart-legend { display: flex; gap: 12px; font-weight: 400; color: var(--app-muted); }
.chart-key { display: inline-flex; align-items: center; gap: 5px; }
.chart-key i { width: 8px; height: 8px; border-radius: 2px; }
.chart svg { display: block; width: 100%; height: auto; margin-top: 6px; overflow: visible; }
.chart .grid { stroke: var(--app-line); stroke-dasharray: 2 3; }
.chart .base { stroke: var(--app-muted); }
.chart .axis { fill: var(--app-muted); font-size: 10px; font-family: var(--font-mono); }
.chart .num { fill: var(--app-text); font-size: 10px; font-weight: 600; font-family: var(--font-mono); animation: num-in 0.3s ease both; }
.chart .hi { fill: var(--app-active); }
.chart .lo { fill: color-mix(in srgb, var(--app-muted) 40%, transparent); }
.chart-key i.hi { background: var(--app-active); }
.chart-key i.lo { background: color-mix(in srgb, var(--app-muted) 40%, transparent); }
.chart .bar { transform-box: fill-box; transform-origin: 50% 100%; animation: col-up 0.7s cubic-bezier(0.2, 0.8, 0.2, 1) both; }
.chart .hbar { transform-box: fill-box; transform-origin: 0 50%; animation: bar-out 0.7s cubic-bezier(0.2, 0.8, 0.2, 1) both; }
@keyframes col-up { from { transform: scaleY(0); } }
@keyframes bar-out { from { transform: scaleX(0); } }
@keyframes num-in { from { opacity: 0; } }
@media (prefers-reduced-motion: reduce) { .chart, .chart .bar, .chart .hbar, .chart .num { animation: none; } }
.composer { margin: 0 14px 14px; border: 1px solid var(--app-line); border-radius: 12px; overflow: visible; }
.frame-bar {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 7px 10px;
  border-bottom: 1px solid var(--app-line); background: var(--app-soft); border-radius: 12px 12px 0 0;
}
.fb-label { padding: 2px 9px; border-radius: 999px; background: var(--app-active); color: #ffffff; font-size: 11px; font-weight: 600; }
.fchip {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px 4px 2px 9px; border-radius: 999px;
  border: 1px solid var(--app-line); background: var(--app-bg); font-size: 11px;
}
.fchip button {
  display: inline-grid; place-items: center; width: 24px; height: 24px; margin: -4px -2px -4px 0; border: 0;
  border-radius: 50%; background: none; color: var(--app-muted); font: inherit; font-size: 14px; cursor: pointer;
}
.fchip button:hover { background: var(--app-soft); color: var(--app-text); }
.composer textarea::placeholder { color: var(--app-muted); opacity: 1; }
.composer textarea {
  display: block; width: 100%; padding: 10px 12px; border: 0; resize: none; outline: none;
  background: transparent; color: var(--app-text); font: inherit; font-size: 13px;
}
.composer-row { display: flex; align-items: center; gap: 8px; padding: 0 10px 10px; }
.picker { position: relative; }
.model-btn, .pill {
  display: inline-flex; align-items: center; gap: 7px; padding: 5px 9px; border-radius: 8px;
  border: 1px solid var(--app-line); background: var(--app-bg); color: var(--app-text); font: inherit; font-size: 12px;
}
.model-btn { cursor: pointer; }
.model-btn:hover { border-color: var(--app-active); }
.pill { color: var(--app-muted); white-space: nowrap; overflow: hidden; cursor: pointer; max-width: 100%; }
.pill:hover { border-color: var(--app-active); color: var(--app-text); }
.pill span { overflow: hidden; text-overflow: ellipsis; }
.conn-menu { width: 280px; }
.conn-opt {
  display: flex; align-items: center; gap: 10px; width: 100%; min-height: 40px; padding: 7px 8px; border: 0;
  border-radius: 7px; background: transparent; color: var(--app-text); font: inherit; font-size: 13px;
  text-align: left; cursor: pointer;
}
.conn-opt:hover { background: var(--app-soft); }
.conn-opt .brand-icon { width: 18px; height: 18px; }
.conn-opt span { flex: 1; }
.conn-opt .toggle { width: 30px; height: 18px; margin: 0; background: var(--app-line); }
.conn-opt .toggle b { top: 3px; left: 3px; width: 12px; height: 12px; background: var(--app-muted); transition: left 140ms ease; }
.conn-opt[aria-checked="true"] .toggle { background: var(--app-active); }
.conn-opt[aria-checked="true"] .toggle b { left: 15px; background: #ffffff; }
.logo { width: 16px; height: 16px; flex: none; }
.logo.tile {
  display: inline-grid; place-items: center; border-radius: 4px; background: var(--app-text); color: var(--app-bg);
  font-size: 8px; font-weight: 700; letter-spacing: 0.02em;
}
:root[data-theme="dark"] .logo.mono { filter: invert(1) hue-rotate(180deg); }
.send-btn {
  display: inline-grid; place-items: center; width: 30px; height: 30px; margin-left: auto; border-radius: 50%;
  border: 0; background: var(--app-text); color: var(--app-bg); cursor: pointer;
}
.model-menu {
  position: absolute; left: 0; bottom: calc(100% + 8px); z-index: 5; width: 260px; max-height: 340px; overflow-y: auto;
  padding: 6px; border-radius: 12px; border: 1px solid var(--app-line); background: var(--app-bg);
  box-shadow: 0 16px 40px rgb(0 0 0 / 28%);
}
.menu-note { margin: 6px 8px 8px; font-size: 12px; line-height: 16px; color: var(--app-muted); }
.model-group p { margin: 8px 8px 2px; font-size: 11px; font-weight: 600; color: var(--app-muted); }
.model-opt {
  display: flex; align-items: center; gap: 9px; width: 100%; padding: 7px 8px; border: 0; border-radius: 7px;
  background: transparent; color: var(--app-text); font: inherit; font-size: 13px; text-align: left; cursor: pointer;
}
.model-opt:hover { background: var(--app-soft); }
.model-opt.on { background: var(--app-soft); box-shadow: inset 2px 0 0 var(--app-active); }
.app-frames { padding: 14px; border-left: 1px solid var(--app-line); min-height: 0; overflow-y: auto; }
.pf-title { margin: 0; font-weight: 600; font-size: 14px; }
.pf-sub { margin: 2px 0 12px; color: var(--app-muted); font-size: 12px; }
.pf {
  display: flex; align-items: center; gap: 9px; padding: 9px 10px; margin-bottom: 6px; border-radius: 8px;
  border: 1px solid var(--app-line); cursor: pointer;
}
.pf:has(input:checked) { border-color: var(--app-active); }
.pf input {
  appearance: none; display: grid; place-items: center; flex: none; width: 16px; height: 16px; margin: 0;
  border: 1.5px solid var(--app-muted); border-radius: 4px; cursor: pointer;
}
.pf input:checked { background: var(--app-active); border-color: var(--app-active); }
.pf input:checked::before {
  content: ""; width: 4px; height: 8px; margin-top: -2px;
  border: solid #ffffff; border-width: 0 2px 2px 0; transform: rotate(45deg);
}

.connectors { padding-top: 40px; padding-bottom: 40px; text-align: center; }
.connectors p { margin: 0 0 28px; font-family: 'IBM Plex Mono', var(--font-mono); font-size: 14px; color: var(--muted); }
.connectors ul {
  list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; justify-content: center; gap: 16px 48px;
}
.connectors li { display: inline-flex; padding: 4px; }
.strip-logo { width: 36px; height: 36px; }
:root[data-theme="dark"] .strip-logo.mono { filter: invert(1); }
.brand-icon { width: 20px; height: 20px; flex: none; }
:root[data-theme="dark"] .brand-icon.mono { filter: invert(1); }

.problem h2 { max-width: 14em; }
.pains { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 40px; border-top: 1px solid var(--line); }
.pains > div { padding: 32px 32px 4px; }
.pains > div:first-child { padding-left: 0; }
.pains > div + div { border-left: 1px solid var(--line); }
.pains h3 { display: flex; align-items: center; gap: 10px; }
.pains h3 .icon { color: var(--accent); }
.pains p { margin: 0; color: var(--muted); }

.chapter { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 56px; align-items: center; }
.chapter:nth-of-type(even) .chapter-copy { order: 2; }
.chapter-copy p { text-wrap: pretty; margin: 0 0 20px; color: var(--muted); font-size: 17px; }
.chapter-copy ul { margin: 0; padding: 0; list-style: none; }
.chapter-copy li { padding: 10px 0 10px 22px; border-top: 1px solid var(--line); position: relative; font-size: 15px; }
.chapter-copy li::before {
  content: ""; position: absolute; left: 0; top: 19px; width: 8px; height: 8px; border-radius: 2px; background: var(--blue);
}

/* Chapter illustrations: drawn in HTML, animated once as they scroll into view. */
.art {
  position: relative; border: 1px solid var(--line); border-radius: 16px; background: var(--panel);
  padding: 32px; min-height: 340px;
}
.chip {
  display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 999px; font-size: 13px;
  background: var(--tile); color: var(--accent); border: 1px solid var(--line);
}
.toggle { position: relative; display: block; width: 34px; height: 20px; margin-left: auto; border-radius: 999px; background: var(--line); }
.toggle b { position: absolute; top: 3px; left: 3px; width: 14px; height: 14px; border-radius: 50%; background: var(--bg); }
.fcard.on .toggle { background: var(--blue); }
.fcard.on .toggle b { left: 17px; background: #ffffff; }

.art-frames { display: grid; grid-template-columns: minmax(0, 1fr) 56px minmax(0, 1fr); align-items: center; gap: 0; }
.fcards { display: grid; gap: 10px; }
.fcard {
  display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 10px;
  background: var(--bg); border: 1px solid var(--line); font-size: 14px; font-weight: 500;
}
.fcard .icon-sm { color: var(--faint); }
.fcard.on { border-color: var(--blue); }
.fcard.on .icon-sm { color: var(--accent); }
.fflow { position: relative; height: 2px; margin: 0 8px; }
.fline { position: absolute; inset: 0; background: var(--blue); transform-origin: left; }
.fdot {
  position: absolute; top: -4px; right: -2px; width: 10px; height: 10px; border-radius: 50%; background: var(--blue);
}
.newchat {
  display: grid; gap: 12px; padding: 16px; border-radius: 12px; background: var(--bg); border: 1px solid var(--line);
}
.nc-head { display: flex; justify-content: space-between; font-weight: 600; font-size: 15px; }
.nc-head span { font-family: var(--font-mono); font-size: 12px; font-weight: 400; color: var(--accent); }
.nc-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.nc-input {
  display: flex; align-items: center; justify-content: space-between; padding: 10px 10px 10px 12px;
  border-radius: 10px; border: 1px solid var(--line); font-size: 13px; color: var(--faint);
}
.send { width: 22px; height: 22px; border-radius: 6px; background: var(--blue); }

.art-tools { display: grid; gap: 20px; padding: 28px; }
.stage { position: relative; aspect-ratio: 5 / 3; }
.spokes { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.spokes line { stroke: var(--line); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.spokes line.live { stroke: var(--blue); stroke-dasharray: 3 4; }
.tool, .core, .ping { position: absolute; transform: translate(-50%, -50%); }
.ping { z-index: 1; } .tool { z-index: 2; } .core { z-index: 3; }
.tool {
  display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; white-space: nowrap;
  border-radius: 10px; background: var(--bg); border: 1px solid var(--line); font-size: 13px; font-weight: 500;
}
.tool.live { border-color: var(--blue); }
.core {
  display: grid; place-items: center; width: 76px; height: 76px; border-radius: 50%;
  background: var(--bg); border: 1px solid var(--line); box-shadow: 0 0 0 8px var(--tile);
}
.core img { display: block; width: 40px; height: 40px; }
.ping { width: 9px; height: 9px; border-radius: 50%; background: var(--blue); opacity: 0; }
.call {
  justify-self: center; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; padding: 10px 14px;
  border-radius: 10px; background: var(--bg); border: 1px solid var(--line); font-size: 12px; color: var(--muted);
}
.call-name { display: inline-flex; gap: 10px; }
.call code { font-family: var(--font-mono); color: var(--accent); }
.call .ok { color: var(--text); font-weight: 500; }
"""
    + "".join(f".t{i} {{ left: {x}%; top: {y}%; }}\n" for i, (_, x, y) in enumerate(_TOOL_NODES))
    + ".hot {{ left: {left}%; width: {width}%; height: {height}%; }}\n".format(**_SIDEBAR_ENTRY)
    + "".join(f".hot-{view} {{ top: {top}%; }}\n" for view, _, top in _VIEWS)
    + f".core, .ping {{ left: {_CORE_NODE[0]}%; top: {_CORE_NODE[1]}%; }}\n"
    + """
.boundary {
  position: relative; padding: 36px 24px 24px; border: 1.5px dashed var(--blue); border-radius: 14px;
}
.blabel {
  position: absolute; top: -11px; left: 18px; padding: 0 8px; background: var(--panel);
  font-family: var(--font-mono); font-size: 12px; color: var(--accent);
}
.htiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.htile {
  display: grid; justify-items: center; gap: 8px; padding: 18px 8px; border-radius: 10px; text-align: center;
  background: var(--bg); border: 1px solid var(--line); font-size: 13px; font-weight: 500; color: var(--accent);
}
.htile span { color: var(--text); }
.usage { display: grid; gap: 10px; margin-top: 22px; }
.ulabel { font-family: var(--font-mono); font-size: 12px; color: var(--faint); }
.ubar { display: grid; grid-template-columns: 48px minmax(0, 1fr); align-items: center; gap: 10px; font-size: 13px; }
.ubar i { display: block; height: 10px; border-radius: 5px; background: var(--blue); transform-origin: left; }
.ubar .b0 { width: 46%; } .ubar .b1 { width: 82%; opacity: 0.55; }

@supports (animation-timeline: view()) {
  /* One timeline per illustration: its own passage through the viewport. Each
     sequence starts once the illustration is in view and ends as it reaches
     the middle of the screen, where it is being looked at. */
  .art { view-timeline-name: --art; }
  .art * { animation-timeline: --art; animation-fill-mode: both; animation-timing-function: ease-out; }
  @keyframes knob-on { from { left: 3px; background: var(--bg); } }
  @keyframes track-on { from { background: var(--line); } }
  @keyframes edge-on { from { border-color: var(--line); } }
  @keyframes draw-x { from { transform: scaleX(0); } }
  @keyframes appear { from { opacity: 0; transform: translateY(8px); } }
  @keyframes travel {
    0% { opacity: 1; left: CXX%; top: CYY%; }
    50% { opacity: 1; left: DXX%; top: DYY%; }
    100% { opacity: 0; left: CXX%; top: CYY%; }
  }
  @keyframes grow { from { transform: scaleX(0); } }
  .fcard.on .toggle b { animation-name: knob-on; animation-range: cover 28% cover 34%; }
  .fcard.on .toggle { animation-name: track-on; animation-range: cover 28% cover 34%; }
  .fcard.on { animation-name: edge-on; animation-range: cover 28% cover 34%; }
  .fcard.on.s2 .toggle b, .fcard.on.s2 .toggle, .fcard.on.s2 { animation-range: cover 34% cover 40%; }
  .fline { animation-name: draw-x; animation-range: cover 40% cover 45%; }
  .fdot, .nc-chips .c1, .nc-head span { animation-name: appear; animation-range: cover 44% cover 48%; }
  .nc-chips .c2 { animation-name: appear; animation-range: cover 46% cover 50%; }
  .ping { animation-name: travel; animation-range: cover 28% cover 50%; animation-timing-function: linear; }
  .htile { animation-name: appear; animation-range: cover 28% cover 36%; }
  .h1 { animation-range: cover 32% cover 40%; } .h2 { animation-range: cover 36% cover 44%; }
  .ubar i { animation-name: grow; animation-range: cover 40% cover 50%; }
}

.shell { flex: 1; display: flex; width: 100%; }
.topbar-in.wide { max-width: none; padding: 14px 28px; }
/* The account dashboard's sidebar, in the admin panel's shape. Folded, it
   keeps only its icons; the choice is remembered in this browser. */
.dash { flex: 1; display: grid; grid-template-columns: 15rem minmax(0, 1fr); transition: grid-template-columns 0.2s ease; }
.dash-nav {
  position: sticky; top: 65px; align-self: start; height: calc(100vh - 65px);
  display: flex; flex-direction: column; gap: 1.75rem; padding: 1.5rem 1rem; border-right: 1px solid var(--line);
}
.dash-nav ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.dash-nav ul + ul { padding-top: 1.75rem; border-top: 1px solid var(--line); margin-top: -0.875rem; }
.dash-link {
  display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 0.6rem; border-radius: 6px;
  color: var(--text); font-size: 0.9rem; text-decoration: none; white-space: nowrap; overflow: hidden;
}
.dash-link svg { color: var(--muted); }
.dash-link:hover { background: color-mix(in srgb, var(--text) 7%, transparent); }
.dash-link:hover svg { color: var(--text); }
.dash-link[aria-current="page"] { background: color-mix(in srgb, var(--accent) 12%, transparent); color: var(--accent); font-weight: 600; }
.dash-link[aria-current="page"] svg { color: var(--accent); }
.dash-link .mark-img, .dash-link img { width: 16px; height: 16px; flex: none; }
.dash-fold {
  margin-top: auto; align-self: flex-start; display: inline-grid; place-items: center; width: 32px; height: 32px;
  border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--muted); cursor: pointer;
}
.dash-fold:hover { color: var(--text); border-color: var(--muted); }
:root[data-sidebar="folded"] .dash { grid-template-columns: 4.25rem minmax(0, 1fr); }
:root[data-sidebar="folded"] .dash-nav { padding-inline: 0.75rem; }
:root[data-sidebar="folded"] .dash-link { justify-content: center; padding-inline: 0; }
:root[data-sidebar="folded"] .dash-link span { display: none; }
:root[data-sidebar="folded"] .dash-fold { align-self: center; }
:root[data-sidebar="folded"] .dash-fold svg { transform: scaleX(-1); }
@media (prefers-reduced-motion: reduce) { .dash { transition: none; } }
.dash-main { min-width: 0; max-width: 64rem; padding: 2.5rem 2.5rem 4rem; }
.dash-main h1 { font-size: 32px; margin: 0 0 24px; }
.acct-head { display: flex; align-items: center; gap: 20px; margin-bottom: 40px; }
.acct-head .avatar { width: 64px; height: 64px; }
.acct-head h1 { margin: 0; font-size: 36px; }
.acct-head p { margin: 4px 0 0; color: var(--muted); }
.acct-list { margin: 0 0 40px; max-width: 640px; border-top: 1px solid var(--line); }
.acct-list div {
  display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 16px; padding: 16px 0;
  border-bottom: 1px solid var(--line);
}
.acct-list dt { color: var(--muted); }
.acct-list dd { margin: 0; overflow-wrap: anywhere; }
.mono { font-family: var(--font-mono); font-size: 14px; }
.acct-actions { display: flex; flex-wrap: wrap; gap: 12px; }
.acct-actions form { margin: 0; }

.ways { background: var(--panel); }
.doors { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 48px; margin-top: 32px; }
.door {
  display: flex; align-items: center; gap: 16px; min-height: 76px; padding: 18px 4px;
  border-top: 1px solid var(--line); text-decoration: none; color: var(--accent);
}
.door .mark-img { transform: scale(1.18); }
.door-text { display: grid; gap: 2px; flex: 1; }
.door-text strong { color: var(--text); font-size: 17px; font-weight: 600; }
.door-text span { color: var(--muted); font-size: 15px; }
.door-arrow { color: var(--faint); transition: transform 160ms cubic-bezier(.2, .7, .2, 1), color 160ms ease; }
.door:hover .door-arrow { color: var(--accent); transform: translateX(4px); }

.closing { text-align: center; padding-top: 96px; padding-bottom: 96px; }
.closing p { margin: 0 auto 28px; max-width: 30em; color: var(--muted); font-size: 18px; }

.footer { background: var(--bg); color: var(--muted); font-size: 15px; border-top: 1px solid var(--line); }
.foot-in {
  display: grid; grid-template-columns: minmax(0, 1.6fr) repeat(3, minmax(0, 1fr)); gap: 48px;
  max-width: 1240px; margin: 0 auto; padding: 72px 64px 56px;
}
.foot-logo { display: inline-flex; align-items: center; gap: 10px; color: var(--text); font-weight: 600; font-size: 18px; text-decoration: none; }
.foot-brand p { margin: 16px 0 24px; max-width: 22em; line-height: 1.6; }
.socials { display: flex; flex-wrap: wrap; gap: 8px; }
.social {
  display: inline-grid; place-items: center; width: 40px; height: 40px; border-radius: 10px;
  border: 1px solid var(--line); color: var(--muted); transition: border-color 140ms ease, color 140ms ease;
}
.social svg { width: 18px; height: 18px; }
.social:hover { border-color: var(--faint); color: var(--text); }
.foot-col h2 { margin: 6px 0 18px; font-size: 14px; font-weight: 600; letter-spacing: 0; color: var(--text); }
.foot-col ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 4px; }
.foot-col a { display: inline-block; padding: 6px 0; color: var(--muted); text-decoration: none; }
.footer a:hover { color: var(--text); }
.foot-base {
  display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px;
  max-width: 1240px; margin: 0 auto; padding: 24px 64px 32px; border-top: 1px solid var(--line);
  font-size: 14px; color: var(--faint);
}
.foot-host { font-family: var(--font-mono); font-size: 13px; }

/* Motion: once, on load. */
@keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
.hero > *, .showcase { animation: rise 600ms cubic-bezier(.2, .7, .2, 1) both; }
.hero > :nth-child(2) { animation-delay: 60ms; }
.hero > :nth-child(3) { animation-delay: 120ms; }
.hero > :nth-child(4) { animation-delay: 180ms; }
.hero > :nth-child(5) { animation-delay: 240ms; }
.showcase { animation-delay: 320ms; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}

@media (max-width: 960px) {
  .foot-in { grid-template-columns: repeat(3, minmax(0, 1fr)); padding: 56px 32px 40px; }
  .foot-brand { grid-column: 1 / -1; }
  .foot-base { padding: 24px 32px 32px; }
  .app-frames { display: none; }
  .app { grid-template-columns: 26% minmax(0, 1fr); }
  .art-frames { grid-template-columns: minmax(0, 1fr); gap: 16px; }
  .fflow { display: none; }
  .nav { display: none; }
  .actions { margin-left: auto; }
  .band { padding: 56px 32px; }
  .chapter { grid-template-columns: minmax(0, 1fr); gap: 32px; }
  .chapter:nth-of-type(even) .chapter-copy { order: 0; }
  .pains { grid-template-columns: minmax(0, 1fr); }
  .pains > div, .pains > div + div { padding: 22px 0 6px; border-left: 0; border-top: 1px solid var(--line); }
  .pains > div:first-child { border-top: 0; }
}
@media (max-width: 560px) {
  .topbar-in { gap: 12px; padding: 12px 16px; }
  .brand span, .who-name { display: none; }
  .theme-toggle { width: 44px; height: 44px; }
  .acct-list div { grid-template-columns: minmax(0, 1fr); gap: 4px; }
  .dash { grid-template-columns: minmax(0, 1fr); align-content: start; }
  .topbar-in.wide { padding: 12px 16px; }
  .dash, :root[data-sidebar="folded"] .dash { grid-template-columns: minmax(0, 1fr); }
  .dash-nav, :root[data-sidebar="folded"] .dash-nav {
    position: static; height: auto; flex-direction: row; gap: 6px; padding: 8px 16px;
    overflow-x: auto; border-right: 0; border-bottom: 1px solid var(--line);
  }
  .dash-nav ul { flex-direction: row; gap: 6px; }
  .dash-nav ul + ul { padding: 0 0 0 6px; margin: 0; border-top: 0; border-left: 1px solid var(--line); }
  :root[data-sidebar="folded"] .dash-link { padding-inline: 0.6rem; }
  :root[data-sidebar="folded"] .dash-link span { display: inline; }
  .dash-fold { display: none; }
  .dash-main { padding: 24px 16px 64px; }
  .dash-link { min-height: 44px; }
  .frame { border: 0; }
  .band { padding: 44px 16px; }
  .hero { padding-top: 56px; }
  .hero-actions .btn { flex: 1 1 100%; }
  .lead { font-size: 17px; }
  .hot { display: none; }
  .tabs { width: 100%; }
  .tab { flex: 1; padding: 10px 6px; text-align: center; }
  .wide { display: none; }
  .send-btn { width: 40px; height: 40px; }
  .model-btn, .new-btn { min-height: 36px; }
  .foot-in { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 36px 24px; padding: 48px 16px 32px; }
  .foot-col a { padding: 10px 0; }
  .foot-base { padding: 20px 16px 28px; }
  .social { width: 44px; height: 44px; }
  .brand { min-height: 44px; }
  .menu-item { min-height: 44px; }
  .doors { grid-template-columns: minmax(0, 1fr); }
  .app { aspect-ratio: auto; height: 560px; grid-template-columns: minmax(0, 1fr); }
  .app-side, .app-frames, .head-actions .ghost-btn, .pill { display: none; }
  .art { padding: 20px; }
  .tool span { display: none; }
  .htile { padding: 14px 6px; font-size: 12px; }
}
"""
)
# The ping travels between the core and Drive, at the diagram's own coordinates.
for _token, _value in (
    ("CXX", _CORE_NODE[0]),
    ("CYY", _CORE_NODE[1]),
    ("DXX", _TOOL_NODES[1][1]),
    ("DYY", _TOOL_NODES[1][2]),
):
    HOME_STYLESHEET = HOME_STYLESHEET.replace(_token, str(_value))
"""The home page's own stylesheet, served from :data:`~.surface.HOME_STYLE_PATH`."""
