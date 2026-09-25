"""Every test that stands in for an external provider says so - and no other does.

No test in this suite calls OpenAI or Anthropic: each one that involves a provider
replaces its network call, hands the code a fake client, or builds the SDK's error
objects offline. That proves how *our* code handles a response. It says nothing
about how a real provider behaves (whether a model id exists, what a real reply
looks like, how long it takes, what it costs), so those tests carry
``pytest.mark.provider_mocked`` and can be selected - or excluded - with
``pytest -m provider_mocked``.

This guard keeps the label true in both directions: a new module that fakes a
provider must be marked, and a marked module must actually fake one (a label on
tests that do not is as misleading as a missing one).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

# What faking a provider looks like in this suite: patching the SDK's call, building
# an SDK client, the in-process stubs, overriding the provider dependencies, or
# importing the SDKs to construct their error objects.
_STANDS_IN_FOR_A_PROVIDER = re.compile(
    r"(?m)"
    r"(\b(messages\.create|embeddings\.create|AsyncAnthropic|AsyncOpenAI|"
    r"_StubLLM|_StubEmbeddings|stub_llm|stub_embeddings|get_llm|get_embeddings|"
    r"leaky_anthropic|leaky_openai)\b"
    r"|^(import|from) (anthropic|openai)\b)"
)
_MARKED = re.compile(r"(?m)^pytestmark\s*=\s*pytest\.mark\.provider_mocked\s*$")

# This file describes the patterns above, so it would match its own detector.
_EXEMPT = {Path(__file__).name}


def _test_modules() -> list[Path]:
    return sorted(path for path in TESTS.glob("test_*.py") if path.name not in _EXEMPT)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_test_modules_are_found() -> None:
    assert len(_test_modules()) >= 30


def test_at_least_one_module_is_marked_so_the_guard_cannot_pass_vacuously() -> None:
    assert any(_MARKED.search(_text(path)) for path in _test_modules())


@pytest.mark.parametrize("path", _test_modules(), ids=lambda path: path.name)
def test_a_module_that_stands_in_for_a_provider_is_marked_and_no_other_is(path: Path) -> None:
    source = _text(path)
    stands_in = _STANDS_IN_FOR_A_PROVIDER.search(source) is not None
    marked = _MARKED.search(source) is not None

    if stands_in:
        assert marked, (
            f"{path.name} fakes an external provider but is not marked: add "
            "`pytestmark = pytest.mark.provider_mocked`."
        )
    else:
        assert not marked, (
            f"{path.name} is marked provider_mocked but no longer fakes a provider: "
            "remove the mark (it would overstate what is mocked)."
        )
