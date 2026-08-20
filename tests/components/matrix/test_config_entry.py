"""Tests for UI-managed Matrix account runtime and delivery."""

from pathlib import Path
from unittest.mock import ANY, AsyncMock, call, patch

from nio.responses import Response, UploadError, UploadResponse
from PIL import Image
import pytest

from homeassistant.components.matrix import MatrixConfigEntry
from homeassistant.components.matrix.client import (
    MatrixAuthenticationError,
    MatrixConnectionError,
)
from homeassistant.components.matrix.const import (
    ATTR_FORMAT,
    ATTR_IMAGES,
    ATTR_MESSAGE_ID,
    ATTR_REACTION,
    ATTR_ROOM,
    ATTR_THREAD_ID,
    DOMAIN,
    FORMAT_HTML,
    SERVICE_REACT,
    SERVICE_SEND_MESSAGE,
)
from homeassistant.components.notify import ATTR_DATA, ATTR_MESSAGE, ATTR_TARGET
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_ACCESS_TOKEN,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component

from .conftest import (
    TEST_HOMESERVER,
    TEST_MXID,
    TEST_PASSWORD,
    TEST_ROOM_A_ID,
    TEST_ROOM_B_ALIAS,
    TEST_ROOM_B_ID,
    TEST_TOKEN,
)

from tests.common import MockConfigEntry

ENTRY_DATA = {
    "homeserver": "https://matrix.example.com",
    CONF_USERNAME: TEST_MXID,
    CONF_PASSWORD: TEST_PASSWORD,
    CONF_VERIFY_SSL: True,
    CONF_ACCESS_TOKEN: TEST_TOKEN,
}


async def _async_setup_entry(hass: HomeAssistant) -> MatrixConfigEntry:
    """Set up and return a UI-managed Matrix config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_MXID,
        unique_id=TEST_MXID.lower(),
        data=ENTRY_DATA,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entry_lifecycle(hass: HomeAssistant, mock_client: object) -> None:
    """Test an outbound client starts once and closes on unload."""
    entry = await _async_setup_entry(hass)
    close = AsyncMock()
    entry.runtime_data._client.close = close

    assert await hass.config_entries.async_unload(entry.entry_id)
    close.assert_awaited_once_with()


async def test_expired_token_falls_back_to_password(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test setup replaces an expired access token after password login."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=TEST_MXID,
        unique_id=TEST_MXID.lower(),
        data=ENTRY_DATA | {CONF_ACCESS_TOKEN: "expired"},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.data[CONF_ACCESS_TOKEN] == TEST_TOKEN


async def test_entry_preserves_custom_title(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test setup does not overwrite a user-renamed config entry title."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="My Matrix account",
        unique_id=TEST_MXID.lower(),
        data=ENTRY_DATA,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.title == "My Matrix account"


@pytest.mark.parametrize(
    ("error", "expected_state"),
    [
        pytest.param(
            MatrixAuthenticationError(), ConfigEntryState.SETUP_ERROR, id="invalid-auth"
        ),
        pytest.param(
            MatrixConnectionError(), ConfigEntryState.SETUP_RETRY, id="cannot-connect"
        ),
    ],
)
async def test_entry_setup_error(
    hass: HomeAssistant,
    error: Exception,
    expected_state: ConfigEntryState,
) -> None:
    """Test setup translates Matrix client errors for config entries."""
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA)
    entry.add_to_hass(hass)

    with patch("homeassistant.components.matrix.async_login_client", side_effect=error):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is expected_state


async def test_outbound_delivery(
    hass: HomeAssistant,
    mock_client: object,
    mock_allowed_path: object,
    tmp_path: Path,
) -> None:
    """Test text, HTML, threads, images, aliases, and reactions."""
    entry = await _async_setup_entry(hass)
    client = entry.runtime_data._client
    room_send = AsyncMock(return_value=Response())
    client.room_send = room_send
    client.upload = AsyncMock(
        return_value=(
            UploadResponse(content_uri=f"mxc://{TEST_HOMESERVER}/image"),
            None,
        )
    )
    image_path = tmp_path / "test.png"
    Image.new("RGBA", size=(50, 50), color=(255, 0, 0)).save(image_path)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_MESSAGE: "<b>Test</b>",
            ATTR_TARGET: [TEST_ROOM_A_ID, TEST_ROOM_B_ALIAS],
            ATTR_DATA: {
                ATTR_FORMAT: FORMAT_HTML,
                ATTR_THREAD_ID: "thread_id",
                ATTR_IMAGES: [str(image_path)],
            },
        },
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_REACT,
        {
            ATTR_CONFIG_ENTRY_ID: entry.entry_id,
            ATTR_REACTION: "👍",
            ATTR_ROOM: TEST_ROOM_B_ALIAS,
            ATTR_MESSAGE_ID: "message_id",
        },
        blocking=True,
    )

    text_content = {
        "msgtype": "m.text",
        "body": "<b>Test</b>",
        "format": "org.matrix.custom.html",
        "formatted_body": "<b>Test</b>",
        "m.relates_to": {"event_id": "thread_id", "rel_type": "m.thread"},
    }
    image_content = {
        "body": "test.png",
        "info": {
            "size": image_path.stat().st_size,
            "mimetype": "image/png",
            "w": 50,
            "h": 50,
        },
        "msgtype": "m.image",
        "url": f"mxc://{TEST_HOMESERVER}/image",
        "m.relates_to": {"event_id": "thread_id", "rel_type": "m.thread"},
    }
    reaction_content = {
        "m.relates_to": {
            "event_id": "message_id",
            "key": "👍",
            "rel_type": "m.annotation",
        }
    }
    room_send.assert_has_awaits(
        [
            call(
                room_id=TEST_ROOM_A_ID,
                message_type="m.room.message",
                content=text_content,
            ),
            call(
                room_id=TEST_ROOM_B_ID,
                message_type="m.room.message",
                content=text_content,
            ),
            call(
                room_id=TEST_ROOM_A_ID,
                message_type="m.room.message",
                content=image_content,
            ),
            call(
                room_id=TEST_ROOM_B_ID,
                message_type="m.room.message",
                content=image_content,
            ),
            call(
                room_id=TEST_ROOM_B_ID,
                message_type="m.reaction",
                content=reaction_content,
            ),
        ],
        any_order=True,
    )
    assert room_send.await_count == 5
    client.upload.assert_awaited_once_with(
        ANY,
        content_type="image/png",
        filename="test.png",
        filesize=image_path.stat().st_size,
    )


async def test_sole_account_is_used_without_selector(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test an omitted selector resolves the sole loaded Matrix account."""
    entry = await _async_setup_entry(hass)
    room_send = AsyncMock(return_value=Response())
    entry.runtime_data._client.room_send = room_send

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {ATTR_MESSAGE: "Test", ATTR_TARGET: [TEST_ROOM_A_ID]},
        blocking=True,
    )

    room_send.assert_awaited_once_with(
        room_id=TEST_ROOM_A_ID,
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": "Test"},
    )


async def test_delivery_failure_is_translated(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test UI-managed delivery failures are returned to the caller."""
    entry = await _async_setup_entry(hass)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MESSAGE: "Test",
                ATTR_TARGET: ["!unknown:example.com"],
            },
            blocking=True,
        )

    assert err.value.translation_key == "delivery_failed"


@pytest.mark.parametrize(
    ("room", "mock_kwargs", "translation_key"),
    [
        pytest.param(
            TEST_ROOM_B_ALIAS,
            {"side_effect": OSError()},
            "delivery_failed",
            id="alias-connection-error",
        ),
        pytest.param(
            TEST_ROOM_B_ALIAS,
            {"return_value": Response()},
            "room_not_found",
            id="alias-not-found",
        ),
    ],
)
async def test_room_alias_failure(
    hass: HomeAssistant,
    mock_client: object,
    room: str,
    mock_kwargs: dict[str, object],
    translation_key: str,
) -> None:
    """Test room alias failures are translated for the caller."""
    entry = await _async_setup_entry(hass)
    entry.runtime_data._client.room_resolve_alias = AsyncMock(**mock_kwargs)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MESSAGE: "Test",
                ATTR_TARGET: [room],
            },
            blocking=True,
        )

    assert err.value.translation_key == translation_key


async def test_room_send_connection_failure(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test a room send transport failure is translated for the caller."""
    entry = await _async_setup_entry(hass)
    entry.runtime_data._client.room_send = AsyncMock(side_effect=OSError)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                ATTR_CONFIG_ENTRY_ID: entry.entry_id,
                ATTR_MESSAGE: "Test",
                ATTR_TARGET: [TEST_ROOM_A_ID],
            },
            blocking=True,
        )

    assert err.value.translation_key == "delivery_failed"


async def test_image_path_not_allowed(
    hass: HomeAssistant, mock_client: object, tmp_path: Path
) -> None:
    """Test an image outside allowed paths is rejected."""
    entry = await _async_setup_entry(hass)
    image_path = tmp_path / "test.png"

    with (
        patch.object(hass.config, "is_allowed_path", return_value=False),
        pytest.raises(ServiceValidationError) as err,
    ):
        await entry.runtime_data.async_send_message(
            "Test", [TEST_ROOM_A_ID], {ATTR_IMAGES: [str(image_path)]}
        )

    assert err.value.translation_key == "path_not_allowed"


@pytest.mark.parametrize(
    "mock_kwargs",
    [
        pytest.param({"side_effect": OSError()}, id="connection-error"),
        pytest.param(
            {"return_value": (UploadError(message="Upload failed"), None)},
            id="upload-error",
        ),
        pytest.param({"return_value": (Response(), None)}, id="unexpected-response"),
    ],
)
async def test_image_upload_failure(
    hass: HomeAssistant,
    mock_client: object,
    mock_allowed_path: object,
    tmp_path: Path,
    mock_kwargs: dict[str, object],
) -> None:
    """Test image upload failures are translated for the caller."""
    entry = await _async_setup_entry(hass)
    entry.runtime_data._client.upload = AsyncMock(**mock_kwargs)
    image_path = tmp_path / "test.png"
    Image.new("RGBA", size=(1, 1)).save(image_path)

    with pytest.raises(ServiceValidationError) as err:
        await entry.runtime_data.async_send_message(
            "Test", [TEST_ROOM_A_ID], {ATTR_IMAGES: [str(image_path)]}
        )

    assert err.value.translation_key == "upload_failed"


async def test_multiple_accounts_require_selector(
    hass: HomeAssistant, mock_client: object
) -> None:
    """Test an omitted selector is rejected when multiple accounts are loaded."""
    await _async_setup_entry(hass)
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        title="@other:example.com",
        unique_id="@other:example.com",
        data=ENTRY_DATA | {CONF_USERNAME: "@other:example.com"},
    )
    second_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second_entry.entry_id)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {ATTR_MESSAGE: "Test", ATTR_TARGET: [TEST_ROOM_A_ID]},
            blocking=True,
        )

    assert err.value.translation_key == "service_found_multiple_config_entry_for_domain"


async def test_no_account_requires_configuration(hass: HomeAssistant) -> None:
    """Test an omitted selector is rejected when no account or YAML bot exists."""
    assert await async_setup_component(hass, DOMAIN, {})

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {ATTR_MESSAGE: "Test", ATTR_TARGET: [TEST_ROOM_A_ID]},
            blocking=True,
        )

    assert err.value.translation_key == "service_found_no_config_entry_for_domain"
