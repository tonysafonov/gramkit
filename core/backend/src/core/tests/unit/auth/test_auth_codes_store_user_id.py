"""The Telegram one-time code and the email password reset store the user id as a string.

Regression: both flows put ``user.id`` (a UUID) into a dict for ``json.dumps``, which can't serialize a UUID. Sending a
Telegram login code answered "Failed to send verification code" for every existing user, and starting a password reset
raised a 500 for every real account. Now the id is stored as a string and read back as a UUID.

Runs without the ``app`` package (core CI): the repos are fakes, Redis is the in-memory one.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from core.services import auth as auth_module
from core.services.auth import AUTH_CODE_PREFIX, AuthService, EmailRedisKeys
from core.testing.fixtures.redis import InMemoryRedis

pytestmark = pytest.mark.regression


def _service(user) -> tuple[AuthService, InMemoryRedis, SimpleNamespace]:
    redis = InMemoryRedis()
    users = SimpleNamespace(
        get_by_tg_username=AsyncMock(return_value=user),
        get_user_by_id=AsyncMock(return_value=user),
        get_by_verified_email=AsyncMock(return_value=user),
        get_by_id=AsyncMock(return_value=user),
        session=SimpleNamespace(flush=AsyncMock()),
    )
    service = AuthService(
        repo=SimpleNamespace(users=users),
        producer=None,
        services=SimpleNamespace(redis=redis, sessions=SimpleNamespace(create_session=AsyncMock(return_value="sid"))),
        bot=None,
    )
    return service, redis, users


@pytest.mark.anyio
async def test_telegram_code_send_then_verify_round_trips_the_user_id(monkeypatch):
    # the response schema is not what this test is about
    monkeypatch.setattr(auth_module, "UserSchema", SimpleNamespace(model_validate=lambda u: u))
    user = SimpleNamespace(id=uuid4(), telegram_id=42, username="ann", user_type=SimpleNamespace(value="regular"))
    service, redis, users = _service(user)

    sent = await service.send_verification_code("@Ann")

    assert sent["success"] is True
    stored = json.loads(await redis.get(f"{AUTH_CODE_PREFIX}ann"))
    assert stored["user_id"] == str(user.id)

    verified = await service.verify_code("ann", sent["code"])

    assert verified["success"] is True
    users.get_user_by_id.assert_awaited_once_with(user.id)
    assert isinstance(users.get_user_by_id.await_args.args[0], UUID)


@pytest.mark.anyio
async def test_password_reset_start_then_complete_round_trips_the_user_id():
    user = SimpleNamespace(id=uuid4(), display_name="Ann", password_hash="old")
    service, redis, users = _service(user)
    service._send_email = AsyncMock(return_value=True)

    started = await service.email_start_reset("ann@example.com")

    stored = json.loads(await redis.get(EmailRedisKeys.RESET.format(token=started["reset_token"])))
    assert stored["user_id"] == str(user.id)

    await service.email_complete_reset(started["reset_token"], stored["code"], "A-new-passw0rd!")

    users.get_by_id.assert_awaited_once_with(user.id)
    assert isinstance(users.get_by_id.await_args.args[0], UUID)
    assert user.password_hash != "old"
