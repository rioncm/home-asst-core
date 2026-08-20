"""Config flow for Matrix."""

from collections.abc import Mapping
import re
from typing import Any, override

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import MatrixAuthenticationError, MatrixConnectionError, async_login_client
from .const import CONF_HOMESERVER, CONF_USERNAME_REGEX, DOMAIN


def _account_schema(
    values: Mapping[str, Any] | None = None,
    *,
    password_required: bool = True,
) -> vol.Schema:
    """Return the Matrix account schema."""
    values = values or {}
    password_key = (
        vol.Required(CONF_PASSWORD)
        if password_required
        else vol.Optional(CONF_PASSWORD)
    )
    return vol.Schema(
        {
            vol.Required(
                CONF_HOMESERVER,
                description={
                    "suggested_value": values.get(CONF_HOMESERVER, "https://")
                },
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Required(
                CONF_USERNAME,
                description={"suggested_value": values.get(CONF_USERNAME, "")},
            ): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
            ),
            password_key: TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_VERIFY_SSL,
                default=values.get(CONF_VERIFY_SSL, True),
            ): bool,
        }
    )


def _valid_username(username: str) -> bool:
    """Return whether the username is a full Matrix user ID."""
    return re.fullmatch(CONF_USERNAME_REGEX, username) is not None


async def _async_validate_account(
    user_input: dict[str, Any],
) -> tuple[str, str]:
    """Validate Matrix credentials and return identity and access token."""
    try:
        homeserver = cv.url(user_input[CONF_HOMESERVER])
    except vol.Invalid as err:
        raise MatrixConnectionError from err
    client, user_id, access_token = await async_login_client(
        homeserver,
        user_input[CONF_VERIFY_SSL],
        user_input[CONF_USERNAME],
        user_input[CONF_PASSWORD],
    )
    await client.close()
    return user_id, access_token


class MatrixConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Matrix."""

    VERSION = 1

    async def _async_entry_from_input(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult | None:
        """Validate input and create an entry when valid."""
        await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
        self._abort_if_unique_id_configured()

        try:
            user_id, access_token = await _async_validate_account(user_input)
        except MatrixAuthenticationError:
            return self.async_show_form(
                step_id=self.context["source"],
                data_schema=_account_schema(user_input),
                errors={"base": "invalid_auth"},
            )
        except MatrixConnectionError:
            return self.async_show_form(
                step_id=self.context["source"],
                data_schema=_account_schema(user_input),
                errors={"base": "cannot_connect"},
            )

        await self.async_set_unique_id(user_id.lower())
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=user_id,
            data=user_input | {CONF_ACCESS_TOKEN: access_token},
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not _valid_username(user_input[CONF_USERNAME]):
                errors[CONF_USERNAME] = "invalid_username"
            else:
                result = await self._async_entry_from_input(user_input)
                if result is not None:
                    return result

        return self.async_show_form(
            step_id="user", data_schema=_account_schema(user_input), errors=errors
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Import a Matrix account from YAML."""
        account_data = {
            CONF_HOMESERVER: import_data[CONF_HOMESERVER],
            CONF_USERNAME: import_data[CONF_USERNAME],
            CONF_PASSWORD: import_data[CONF_PASSWORD],
            CONF_VERIFY_SSL: import_data[CONF_VERIFY_SSL],
        }
        await self.async_set_unique_id(account_data[CONF_USERNAME].lower())
        self._abort_if_unique_id_configured(updates=account_data)

        try:
            user_id, access_token = await _async_validate_account(account_data)
        except MatrixAuthenticationError:
            return self.async_abort(reason="invalid_auth")
        except MatrixConnectionError:
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(user_id.lower())
        self._abort_if_unique_id_configured(updates=account_data)
        return self.async_create_entry(
            title=user_id,
            data=account_data | {CONF_ACCESS_TOKEN: access_token},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauthentication."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            account_data = dict(entry.data) | {CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                user_id, access_token = await _async_validate_account(account_data)
            except MatrixAuthenticationError:
                errors["base"] = "invalid_auth"
            except MatrixConnectionError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_id.lower())
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_ACCESS_TOKEN: access_token,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure a Matrix account."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not _valid_username(user_input[CONF_USERNAME]):
                errors[CONF_USERNAME] = "invalid_username"
            else:
                account_data = dict(entry.data) | user_input
                if not user_input.get(CONF_PASSWORD):
                    account_data[CONF_PASSWORD] = entry.data[CONF_PASSWORD]
                else:
                    account_data.pop(CONF_ACCESS_TOKEN, None)
                try:
                    user_id, access_token = await _async_validate_account(account_data)
                except MatrixAuthenticationError:
                    errors["base"] = "invalid_auth"
                except MatrixConnectionError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(user_id.lower())
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        entry,
                        data=account_data | {CONF_ACCESS_TOKEN: access_token},
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_account_schema(
                user_input or entry.data,
                password_required=False,
            ),
            errors=errors,
        )
