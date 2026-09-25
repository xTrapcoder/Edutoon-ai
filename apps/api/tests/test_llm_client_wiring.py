"""Two LLM clients, two models: where each is built and which one a caller gets.

``LLM_MODEL`` serves claim judging and topic extraction; ``SCRIPT_LLM_MODEL`` writes
scripts. Both the API (its lifespan) and the worker (``build_worker_providers``)
build one client per model from the same settings, so the setting really does choose
the model that writes a script. The models below are made up and differ, so nothing
here passes because both happened to be the default.

No client is ever called - only constructed - so there is no network and no provider;
the tests are marked ``provider_mocked`` because they build (never use) real SDK clients.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from anthropic import AsyncAnthropic
from fastapi import FastAPI

import edutoon.main as main_module
from edutoon.api.dependencies import get_llm, get_script_llm
from edutoon.core.config import Settings, get_settings
from edutoon.providers.llm import LLM
from edutoon.worker.dispatch import build_worker_providers

# Stands in for an external provider (OpenAI / Anthropic): these tests prove how our code
# handles a response, never how a real provider behaves. See "Provider-mocked tests"
# in apps/api/README.md.
pytestmark = pytest.mark.provider_mocked

GENERAL_MODEL = "general-model-x"
SCRIPT_MODEL = "script-model-y"


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(
        update={"LLM_MODEL": GENERAL_MODEL, "SCRIPT_LLM_MODEL": SCRIPT_MODEL, **overrides}
    )


def test_the_worker_binds_each_llm_client_to_its_own_model() -> None:
    providers = build_worker_providers(_settings())

    assert providers.llm.model == GENERAL_MODEL
    assert providers.script_llm.model == SCRIPT_MODEL
    assert providers.script_llm is not providers.llm


def test_the_worker_follows_the_settings_when_the_two_models_are_the_same() -> None:
    """Equal models still mean two clients - one per task - not one shared object."""
    providers = build_worker_providers(_settings(SCRIPT_LLM_MODEL=GENERAL_MODEL))

    assert providers.llm.model == providers.script_llm.model == GENERAL_MODEL
    assert providers.script_llm is not providers.llm


def test_the_worker_defaults_come_from_the_documented_settings() -> None:
    settings = get_settings()

    providers = build_worker_providers(settings)

    assert providers.llm.model == settings.LLM_MODEL
    assert providers.script_llm.model == settings.SCRIPT_LLM_MODEL


async def test_the_api_lifespan_builds_a_separate_client_for_the_script_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()

    async def _no_engine_to_dispose() -> None:
        return None

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    # The engine is process-wide and other tests rely on it; the lifespan's own
    # engine handling is not what is under test here.
    monkeypatch.setattr(main_module, "get_engine", lambda: object())
    monkeypatch.setattr(main_module, "dispose_engine", _no_engine_to_dispose)
    app = FastAPI()

    async with main_module.lifespan(app):
        assert app.state.llm.model == GENERAL_MODEL
        assert app.state.script_llm.model == SCRIPT_MODEL
        assert app.state.script_llm is not app.state.llm


def test_the_two_dependencies_hand_out_different_clients() -> None:
    """Claim judging asks for ``get_llm``, script queuing for ``get_script_llm``."""
    general = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=GENERAL_MODEL)
    script = LLM(AsyncAnthropic(api_key="sk-ant-test-not-a-real-key"), model=SCRIPT_MODEL)
    state = SimpleNamespace(llm=general, script_llm=script)
    request: Any = SimpleNamespace(app=SimpleNamespace(state=state))

    assert get_llm(request) is general
    assert get_script_llm(request) is script
