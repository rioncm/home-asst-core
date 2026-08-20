"""Tests for the UI-managed Matrix client."""

from unittest.mock import AsyncMock, MagicMock, patch

from nio.responses import Response
import pytest

from homeassistant.components.matrix.client import (
    MatrixConnectionError,
    async_login_client,
)

from .conftest import TEST_MXID, TEST_PASSWORD


@pytest.mark.parametrize(
    "login",
    [
        pytest.param(AsyncMock(side_effect=OSError()), id="transport-error"),
        pytest.param(AsyncMock(return_value=Response()), id="unexpected-response"),
    ],
)
async def test_login_connection_error(login: AsyncMock) -> None:
    """Test login transport and unexpected response failures."""
    client = MagicMock(login=login, close=AsyncMock())

    with (
        patch(
            "homeassistant.components.matrix.client.AsyncClient", return_value=client
        ),
        pytest.raises(MatrixConnectionError),
    ):
        await async_login_client(
            "https://matrix.example.com",
            True,
            TEST_MXID,
            TEST_PASSWORD,
        )

    client.close.assert_awaited_once_with()
