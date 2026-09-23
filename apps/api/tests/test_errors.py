from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from edutoon.core.errors import NotFoundError
from edutoon.main import create_app


class _ValidateBody(BaseModel):
    # Module-level, not nested in the test: FastAPI resolves the endpoint's
    # string annotations (this module uses `from __future__ import
    # annotations`) against the function's *module* globals, which can't see
    # a class defined inside the test function.
    model_config = ConfigDict(extra="forbid")
    title: str


def test_app_error_maps_to_its_status_and_an_error_envelope():
    app = create_app()

    @app.get("/_test/not-found")
    async def _raise_not_found() -> None:
        raise NotFoundError("Widget 123 not found.")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/not-found")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Widget 123 not found."}
    }


def test_unexpected_error_maps_to_a_generic_500_envelope():
    app = create_app()

    @app.get("/_test/boom")
    async def _raise_boom() -> None:
        raise RuntimeError("boom")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    # The raw exception message must never leak to the client.
    assert "boom" not in body["error"]["message"]


def test_validation_error_returns_structured_field_errors_not_a_raw_dump():
    app = create_app()

    @app.post("/_test/validate")
    async def _validate(body: _ValidateBody) -> dict[str, str]:
        return {"title": body.title}

    with TestClient(app, raise_server_exceptions=False) as client:
        # `title` has the wrong type *and* `secret` is an unknown field
        # (rule 10) - two distinct field errors from one request.
        response = client.post(
            "/_test/validate", json={"title": 123, "secret": "do-not-leak"}
        )

    assert response.status_code == 422
    body = response.json()
    error = body["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert isinstance(error["message"], str)

    field_errors = error["details"]["field_errors"]
    assert len(field_errors) == 2
    for field_error in field_errors:
        assert set(field_error) == {"path", "message", "type"}
        assert all(isinstance(value, str) for value in field_error.values())

    paths = {field_error["path"] for field_error in field_errors}
    assert paths == {"body.title", "body.secret"}

    # The raw submitted value must never be echoed back to the client.
    assert "do-not-leak" not in response.text
