"""The Telegram Stars HTTP webhook stays dormant unless a secret is configured.

Regression: the endpoint checked the secret only ``if`` one was set, and nothing ever set one (the bot long-polls, so
Telegram never calls this URL). Any POST was processed as a payment confirmation. Now it answers 404 without a
configured secret, 401 with a wrong or missing header, and processes only a request carrying the right secret.

Runs without the ``app`` package (core CI): the services are a fake.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.infrastructure.fastapi.dependencies import get_services

# The routers package imports the rate limiter (slowapi), an app-level dependency that core's own CI doesn't install.
pytest.importorskip("slowapi")
from core.infrastructure.fastapi.routers import webhooks  # noqa: E402

pytestmark = pytest.mark.regression

SECRET = "stars-secret"
PAYLOAD = {"payload": "payment_00000000-0000-0000-0000-000000000001", "successful_payment": {"total_amount": 1}}


def _app(secret: str | None) -> tuple[FastAPI, AsyncMock]:
    process_callback = AsyncMock()
    app = FastAPI()
    app.include_router(webhooks.telegram_stars_router)
    if secret is not None:
        app.state.telegram_webhook_secret = secret
    services = SimpleNamespace(payments=SimpleNamespace(process_callback=process_callback), repo=SimpleNamespace())
    app.dependency_overrides[get_services] = lambda: services
    return app, process_callback


async def _post(app: FastAPI, headers: dict[str, str] | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/telegram-stars/webhook", json=PAYLOAD, headers=headers or {})


@pytest.fixture(autouse=True)
def _no_alerts(monkeypatch):
    monkeypatch.setattr(webhooks, "alert_webhook_failure", AsyncMock())


@pytest.mark.anyio
async def test_no_secret_configured_refuses_and_processes_nothing():
    app, process_callback = _app(secret=None)

    res = await _post(app, {"X-Telegram-Bot-Api-Secret-Token": "anything"})

    assert res.status_code == 404
    process_callback.assert_not_awaited()


@pytest.mark.anyio
async def test_empty_secret_counts_as_not_configured():
    app, process_callback = _app(secret="")

    res = await _post(app)

    assert res.status_code == 404
    process_callback.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [{}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}])
async def test_missing_or_wrong_secret_is_401(headers):
    app, process_callback = _app(secret=SECRET)

    res = await _post(app, headers)

    assert res.status_code == 401
    process_callback.assert_not_awaited()


@pytest.mark.anyio
async def test_right_secret_is_processed():
    app, process_callback = _app(secret=SECRET)

    res = await _post(app, {"X-Telegram-Bot-Api-Secret-Token": SECRET})

    assert res.status_code == 200
    process_callback.assert_awaited_once()
    assert process_callback.await_args.args[0] == PAYLOAD
