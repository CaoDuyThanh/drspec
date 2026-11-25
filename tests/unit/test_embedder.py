"""The embedding wrapper (`drspec.core.embedder`): what is OURS, on a fake `fastembed`.

The real model is a third-party download of about 90 MB: its four checks (384 floats, deterministic, long input, a cached model needs no
network) are in `scripts/check_real_embedder.py`, run by a person when the model or the library changes. Nothing here loads weights or
touches the network.
"""

from __future__ import annotations

import sys
import types

import pytest

from drspec.core import embedder
from drspec.core.embedder import EMBEDDING_DIM, MODEL_NAME, embed_text
from tests.fakes import FakeEmbeddingModel


@pytest.fixture
def loaded(monkeypatch, tmp_path):
    """`fastembed.TextEmbedding` is a fake that records how it was built and answers with `FakeEmbeddingModel` vectors; nothing is loaded."""
    built = []

    class FakeTextEmbedding(FakeEmbeddingModel):
        def __init__(self, model_name, cache_dir, threads=None):
            built.append({"model_name": model_name, "cache_dir": cache_dir, "threads": threads})

    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = FakeTextEmbedding
    hub = types.ModuleType("huggingface_hub")
    hub_utils = types.ModuleType("huggingface_hub.utils")
    hub_utils.disable_progress_bars = lambda: None
    hub.utils = hub_utils
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", hub_utils)
    monkeypatch.setattr(embedder, "_model", None)  # the process-wide model: not loaded yet
    monkeypatch.chdir(tmp_path)  # the project's own old model folder (`_drspec/models`) is looked for from the working folder: an empty one here
    monkeypatch.setenv(embedder.MODEL_DIR_ENV_VAR, str(tmp_path / "models"))
    return built


class TestEmbedText:
    def test_returns_the_models_vector_as_a_list_of_384_floats(self, loaded):
        vector = embed_text("def foo(): return 1")

        assert type(vector) is list and len(vector) == EMBEDDING_DIM == 384
        assert all(isinstance(v, float) for v in vector)

    def test_the_same_text_gives_the_same_vector_and_other_texts_other_ones(self, loaded):
        assert embed_text("charge the card") == embed_text("charge the card")
        assert embed_text("charge the card") != embed_text("open a socket")

    def test_the_model_is_loaded_once_for_any_number_of_calls_from_the_shared_folder(self, loaded, tmp_path):
        for text in ("a", "b", "c"):
            embed_text(text)

        assert loaded == [{"model_name": MODEL_NAME, "cache_dir": str(tmp_path / "models"), "threads": None}]
