import re

import pytest

from collab_hub_execution import (
    BindingResolutionError,
    CapabilityRequirement,
    ContextCog,
    DeclaredCapabilityResolver,
    ModelCog,
)


def test_context_binding_projects_selected_model_without_secret_value():
    context = ContextCog("summarizer", CapabilityRequirement("model", provider="fast"))
    models = (
        ModelCog("fast", "https://fast.example/v1", "small", "secret/fast"),
        ModelCog("accurate", "https://accurate.example/v1", "large", "secret/accurate"),
    )

    binding = DeclaredCapabilityResolver().resolve_model(context, models)

    assert binding.endpoint == "https://fast.example/v1"
    assert binding.model_identifier == "small"
    assert binding.auth_ref == "secret/fast"
    assert "token" not in repr(binding)


def test_swapping_model_cog_changes_only_the_resolved_binding():
    context = ContextCog("summarizer", CapabilityRequirement("model"))
    old = ModelCog("old", "https://old.example/v1", "old-model", "secret/old")
    new = ModelCog("new", "https://new.example/v1", "new-model", "secret/new")

    assert DeclaredCapabilityResolver().resolve_model(context, (old,)) != DeclaredCapabilityResolver().resolve_model(
        context, (new,)
    )


def test_model_resolution_fails_on_ambiguous_or_missing_provider():
    context = ContextCog("summarizer", CapabilityRequirement("model"))
    models = (
        ModelCog("one", "https://one.example", "one", "secret/one"),
        ModelCog("two", "https://two.example", "two", "secret/two"),
    )
    resolver = DeclaredCapabilityResolver()

    with pytest.raises(BindingResolutionError):
        resolver.resolve_model(context, models)
    with pytest.raises(BindingResolutionError):
        resolver.resolve_model(ContextCog("summarizer", CapabilityRequirement("image")), models)


# --- the hub's models: block --------------------------------------------------------------------

MODELS = """
[models.fake]
endpoint = "http://127.0.0.1:8090/v1"
model = "fake-model"
context_window = 8192
max_output_tokens = 1024

[models.claude]
provider = "anthropic"
model = "claude-opus-5-5"
auth_ref = "env:TEST_CLAUDE_KEY"

[cogs]
hermes = "claude"
hello = "fake"
"""


def _block(text=MODELS):
    import tomllib

    from collab_hub_execution.binding import ModelsBlock

    return ModelsBlock.parse(tomllib.loads(text))


def test_each_cog_gets_its_own_model_and_no_other_and_the_key_is_read_when_a_worker_starts():
    block = _block()
    environ = {"TEST_CLAUDE_KEY": "k1"}
    deliver = block.delivery(environ)
    assert deliver("hello", "r", "s:0") == {
        "COLLAB_MODEL_PROVIDER": "openai-compatible", "COLLAB_MODEL_BASE_URL": "http://127.0.0.1:8090/v1",
        "COLLAB_MODEL_NAME": "fake-model", "COLLAB_MODEL_CONTEXT_WINDOW": "8192",
        "COLLAB_MODEL_MAX_OUTPUT_TOKENS": "1024"}
    assert deliver("hermes", "r", "s:0") == {
        "COLLAB_MODEL_PROVIDER": "anthropic", "COLLAB_MODEL_NAME": "claude-opus-5-5", "COLLAB_MODEL_API_KEY": "k1"}
    assert deliver("echo", "r", "s:0") == {}  # a Cog bound to no model gets nothing
    environ["TEST_CLAUDE_KEY"] = "k2"  # rotated: the next worker gets the new one
    assert deliver("hermes", "r", "s:1")["COLLAB_MODEL_API_KEY"] == "k2"


@pytest.mark.parametrize(("text", "message"), [
    ("[models.m]\nmodel = 'x'\n", "needs an endpoint"),
    ("[models.m]\nendpoint = 'http://x'\nmodel = 'x'\ncolour = 'blue'\n", "unknown keys"),
    ("[models.m]\nprovider = 'gemini'\n", "provider is one of"),
    ("[models.m]\nendpoint = 'http://x'\nmodel = 'x'\ncontext_window = 0\n", "positive integer"),
    ("[cogs]\nhermes = 'nope'\n", "which [models] does not name"),
    ("[agents]\n", "unknown tables"),
    ('models = "bad"\n', "[models] is a table"),
    ("models = []\n", "[models] is a table"),
    ("cogs = []\n", "[cogs] is a table"),
    ("[models.m]\nendpoint = 'http://x'\nmodel = 'x'\n[cogs]\nhermes = 1\n", "names a model, as a string"),
])
def test_a_models_block_that_cannot_be_used_is_refused_when_read(text, message):
    from collab_hub_execution.binding import BindingResolutionError

    with pytest.raises(BindingResolutionError, match=re.escape(message)):
        _block(text)


def test_a_key_that_cannot_be_found_is_refused_at_start_not_when_a_worker_needs_it():
    from collab_hub_execution.binding import BindingResolutionError

    block = _block()
    with pytest.raises(BindingResolutionError, match="not set"):
        block.check({})
    with pytest.raises(BindingResolutionError, match="env:NAME"):
        _block(MODELS.replace("env:TEST_CLAUDE_KEY", "vault:claude")).check({"TEST_CLAUDE_KEY": "k"})
    with pytest.raises(BindingResolutionError, match="needs an auth_ref"):
        _block(MODELS.replace('auth_ref = "env:TEST_CLAUDE_KEY"\n', "")).check({})
    block.check({"TEST_CLAUDE_KEY": "k"})
