"""Tests for the Matrix config flow."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous_serialize

from homeassistant import config_entries
from homeassistant.components.matrix.client import (
    MatrixAuthenticationError,
    MatrixConnectionError,
)
from homeassistant.components.matrix.const import CONF_HOMESERVER, DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv

from .conftest import TEST_MXID, TEST_PASSWORD, TEST_TOKEN

from tests.common import MockConfigEntry

ACCOUNT_DATA = {
    CONF_HOMESERVER: "https://matrix.example.com",
    CONF_USERNAME: TEST_MXID,
    CONF_PASSWORD: TEST_PASSWORD,
    CONF_VERIFY_SSL: True,
}


async def test_user_flow(hass: HomeAssistant, mock_client: object) -> None:
    """Test creating a UI-managed Matrix account."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    voluptuous_serialize.convert(
        result["data_schema"], custom_serializer=cv.custom_serializer
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ACCOUNT_DATA
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_MXID
    assert result["data"] == ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN}


async def test_invalid_auth(hass: HomeAssistant, mock_client: object) -> None:
    """Test invalid credentials are reported without exposing the password."""
    password = "secret-invalid-password"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ACCOUNT_DATA | {CONF_PASSWORD: password}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert password not in str(result)


async def test_cannot_connect(
    hass: HomeAssistant,
    mock_client: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test an invalid homeserver URL is rejected without exposing credentials."""
    password = "secret-password"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        ACCOUNT_DATA | {CONF_HOMESERVER: "not a homeserver", CONF_PASSWORD: password},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert password not in caplog.text


async def test_username_requires_full_mxid_before_login(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test alternate login identifiers are rejected before authentication."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    validate_account = AsyncMock()

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        validate_account,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ACCOUNT_DATA | {CONF_USERNAME: "user@example.com"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_USERNAME: "invalid_username"}
    validate_account.assert_not_awaited()


async def test_duplicate_account(hass: HomeAssistant, mock_client: object) -> None:
    """Test duplicate manual setup aborts before creating a Matrix session."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN},
    ).add_to_hass(hass)
    validate_account = AsyncMock()

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        validate_account,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_USER},
            data=ACCOUNT_DATA,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    validate_account.assert_not_awaited()


async def test_yaml_import(hass: HomeAssistant, mock_client: object) -> None:
    """Test importing the outbound account from YAML."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_IMPORT},
        data=ACCOUNT_DATA,
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN}


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        pytest.param(MatrixAuthenticationError(), "invalid_auth", id="invalid-auth"),
        pytest.param(MatrixConnectionError(), "cannot_connect", id="cannot-connect"),
    ],
)
async def test_yaml_import_error(
    hass: HomeAssistant, error: Exception, reason: str
) -> None:
    """Test YAML import reports account validation errors."""
    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=ACCOUNT_DATA,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_yaml_import_existing_account_does_not_login(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test repeated import updates YAML fields without creating a session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN},
    )
    entry.add_to_hass(hass)
    validate_account = AsyncMock()
    changed_data = ACCOUNT_DATA | {
        CONF_HOMESERVER: "https://new-matrix.example.com",
        CONF_PASSWORD: "new-password",
        CONF_VERIFY_SSL: False,
    }

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        validate_account,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=changed_data,
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    validate_account.assert_not_awaited()
    assert entry.data == changed_data | {CONF_ACCESS_TOKEN: TEST_TOKEN}


async def test_reauth(hass: HomeAssistant, mock_client: object) -> None:
    """Test reauth updates the managed password and token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: "expired"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: TEST_PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == TEST_TOKEN


@pytest.mark.parametrize(
    ("error", "flow_error"),
    [
        pytest.param(MatrixAuthenticationError(), "invalid_auth", id="invalid-auth"),
        pytest.param(MatrixConnectionError(), "cannot_connect", id="cannot-connect"),
    ],
)
async def test_reauth_error(
    hass: HomeAssistant, error: Exception, flow_error: str
) -> None:
    """Test reauthentication reports account validation errors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: "expired"},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: TEST_PASSWORD}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": flow_error}


async def test_reconfigure(hass: HomeAssistant, mock_client: object) -> None:
    """Test reconfiguring connection settings for the same account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_MXID,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], ACCOUNT_DATA | {CONF_VERIFY_SSL: False}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_VERIFY_SSL] is False


async def test_reconfigure_preserves_password(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test reconfiguring without re-entering the stored password."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Custom Matrix account",
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    validate_account = AsyncMock(return_value=(TEST_MXID, TEST_TOKEN))

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        validate_account,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            ACCOUNT_DATA | {CONF_PASSWORD: "", CONF_VERIFY_SSL: False},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Custom Matrix account"
    assert entry.data[CONF_PASSWORD] == TEST_PASSWORD
    validate_account.assert_awaited_once_with(
        ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN, CONF_VERIFY_SSL: False}
    )


async def test_reconfigure_validates_new_password(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test a replacement password is validated instead of the stored token."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    validate_account = AsyncMock(return_value=(TEST_MXID, "new-token"))
    new_data = ACCOUNT_DATA | {CONF_PASSWORD: "new-password"}

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        validate_account,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], new_data
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    validate_account.assert_awaited_once_with(new_data)
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.data[CONF_ACCESS_TOKEN] == "new-token"


async def test_reconfigure_invalid_username(hass: HomeAssistant) -> None:
    """Test reconfigure rejects an invalid Matrix user ID before login."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    validate_account = AsyncMock()

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        validate_account,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ACCOUNT_DATA | {CONF_USERNAME: "user@example.com"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_USERNAME: "invalid_username"}
    validate_account.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "flow_error"),
    [
        pytest.param(MatrixAuthenticationError(), "invalid_auth", id="invalid-auth"),
        pytest.param(MatrixConnectionError(), "cannot_connect", id="cannot-connect"),
    ],
)
async def test_reconfigure_error(
    hass: HomeAssistant, error: Exception, flow_error: str
) -> None:
    """Test reconfigure reports account validation errors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        side_effect=error,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ACCOUNT_DATA | {CONF_PASSWORD: "new-password"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": flow_error}


async def test_reconfigure_wrong_account(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test reconfigure cannot replace the configured Matrix identity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_MXID,
        unique_id=TEST_MXID.lower(),
        data=ACCOUNT_DATA | {CONF_ACCESS_TOKEN: TEST_TOKEN},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )

    with patch(
        "homeassistant.components.matrix.config_flow._async_validate_account",
        return_value=("@other:example.com", TEST_TOKEN),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], ACCOUNT_DATA
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
