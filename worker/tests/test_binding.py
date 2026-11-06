"""The binding, as the hub delivers it: COLLAB_MODEL_* variables, or a file."""

import json

import pytest

from collab_hub_cog_worker import WorkerError, load_binding


def test_the_variables_the_controller_delivers_are_the_binding():
    binding = load_binding({"COLLAB_MODEL_BASE_URL": "http://m/v1", "COLLAB_MODEL_NAME": "m",
                            "COLLAB_MODEL_API_KEY": "k", "COLLAB_MODEL_CONTEXT_WINDOW": "8000",
                            "COLLAB_MODEL_MAX_OUTPUT_TOKENS": "500"})
    assert (binding.base_url, binding.model, binding.api_key, binding.context_window, binding.max_output_tokens) == \
        ("http://m/v1", "m", "k", 8000, 500)
    assert binding.bound and binding.record()["resolved_from"] == "environment"
    assert not load_binding({}).bound


def test_a_binding_file_names_its_key_by_reference(tmp_path):
    path = tmp_path / "binding.json"
    path.write_text(json.dumps({"provider": "openai-compatible", "endpoint": "http://m/v1", "model": "m",
                                "auth_ref": "env:MODEL_KEY", "context_window": 1000}))
    binding = load_binding({"COLLAB_BINDING_FILE": str(path), "MODEL_KEY": "secret"})
    assert binding.api_key == "secret" and binding.base_url == "http://m/v1" and binding.context_window == 1000
    assert binding.record()["resolved_from"] == f"file:{path}" and "secret" not in json.dumps(binding.record())
    with pytest.raises(WorkerError, match="names no variable"):
        load_binding({"COLLAB_BINDING_FILE": str(path)})


@pytest.mark.parametrize("value", ["lots", "-1", "0", "1.5"])
def test_a_limit_that_is_not_a_whole_number_of_tokens_is_binding_invalid(value):
    with pytest.raises(WorkerError) as refused:
        load_binding({"COLLAB_MODEL_CONTEXT_WINDOW": value})
    assert refused.value.code == "binding-invalid"


def test_a_binding_file_that_cannot_be_read_is_binding_invalid(tmp_path):
    with pytest.raises(WorkerError) as refused:
        load_binding({"COLLAB_BINDING_FILE": str(tmp_path / "missing.json")})
    assert refused.value.code == "binding-invalid"
