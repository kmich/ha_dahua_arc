from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector

from .area_assignment import decide_zone_areas
from .area_matcher import AreaCandidate
from .client import probe_connection
from .const import (
    CONF_ARC_SERIAL,
    CONF_AREA_MATCH_AREAS,
    CONF_AREA_MATCH_THRESHOLD,
    CONF_AUTO_AREA_MATCH,
    CONF_DHIP_PORT,
    CONF_ENABLE_RESEARCH_FEATURES,
    CONF_HTTP_PORT,
    CONF_PERIODIC_RESYNC,
    CONF_REMATCH_EXISTING,
    CONF_ZONE_AREA_DECISIONS,
    DEFAULT_AREA_MATCH_THRESHOLD,
    DEFAULT_AUTO_AREA_MATCH,
    DEFAULT_DHIP_PORT,
    DEFAULT_ENABLE_RESEARCH_FEATURES,
    DEFAULT_HTTP_PORT,
    DEFAULT_PERIODIC_RESYNC,
    DOMAIN,
    ISSUE_INVENTORY_ERROR,
    ISSUE_NO_PRIMARY_ZONES,
    ISSUE_SERIAL_MISMATCH,
)
from .vendor.dahua.exceptions import LoginError

# Never echo a stored password back to the browser, and mask typing.
_PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(
        type=selector.TextSelectorType.PASSWORD, autocomplete="current-password"
    )
)


async def _validate(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, Any]:
    return await hass.async_add_executor_job(
        probe_connection,
        str(data[CONF_HOST]).strip(),
        int(data[CONF_HTTP_PORT]),
        int(data[CONF_DHIP_PORT]),
        str(data[CONF_USERNAME]),
        str(data[CONF_PASSWORD]),
    )


def _validation_error(exc: Exception) -> str:
    if isinstance(exc, LoginError):
        return "invalid_auth"
    if isinstance(exc, HTTPError):
        return "invalid_auth" if exc.code in (401, 403) else "cannot_connect"
    if isinstance(exc, (OSError, TimeoutError)):
        return "cannot_connect"
    return "unknown"


def _fallback_unique_id(data: Mapping[str, Any]) -> str:
    """Return the pre-serial unique ID used by older config entries."""
    return f"{data[CONF_HOST]}:{data[CONF_DHIP_PORT]}"


def _unique_id_matches_entry(
    entry: ConfigEntry, result: Mapping[str, Any], data: Mapping[str, Any]
) -> bool:
    """Accept current and legacy unique IDs during reauth/reconfigure.

    Pre-public v0.4.x entries may still be keyed by host:DHIP-port (see
    CONTRIBUTING.md "Version history"). New entries prefer the
    ARC serial number. During credential or host maintenance we must verify the
    target hub without rejecting a valid legacy entry just because the probe can
    now read the serial number.
    """
    current = str(entry.unique_id or "")
    serial = str(result.get("serial_number") or "")
    remembered_serial = str(entry.data.get(CONF_ARC_SERIAL) or "")
    if remembered_serial:
        return bool(serial) and remembered_serial == serial
    if serial and current == serial:
        return True
    old_data = dict(entry.data)
    # A pre-public (pre-v0.6) host:port entry has no proven serial yet. Only allow an
    # in-place credential update of the same endpoint; never migrate it to a
    # different hub on a guess. The successful probe records its serial.
    return (
        current == _fallback_unique_id(old_data)
        and _fallback_unique_id(data) == _fallback_unique_id(old_data)
        and bool(serial)
    )


def _connection_schema(
    defaults: Mapping[str, Any] | None = None, *, password_required: bool = True
) -> vol.Schema:
    """Connection form. ``defaults`` never supplies the password."""
    defaults = defaults or {}
    password_key = (
        vol.Required(CONF_PASSWORD)
        if password_required
        else vol.Optional(CONF_PASSWORD)
    )
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")
            ): str,
            password_key: _PASSWORD_SELECTOR,
            vol.Required(
                CONF_HTTP_PORT,
                default=defaults.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required(
                CONF_DHIP_PORT,
                default=defaults.get(CONF_DHIP_PORT, DEFAULT_DHIP_PORT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        }
    )


def _reauth_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")
            ): str,
            vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
        }
    )


def _behavior_schema(values: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_PERIODIC_RESYNC,
                default=values.get(CONF_PERIODIC_RESYNC, DEFAULT_PERIODIC_RESYNC),
            ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
            vol.Required(
                CONF_AUTO_AREA_MATCH,
                default=values.get(CONF_AUTO_AREA_MATCH, DEFAULT_AUTO_AREA_MATCH),
            ): bool,
            vol.Required(
                CONF_ENABLE_RESEARCH_FEATURES,
                default=values.get(
                    CONF_ENABLE_RESEARCH_FEATURES, DEFAULT_ENABLE_RESEARCH_FEATURES
                ),
            ): bool,
        }
    )


def _area_candidates(
    hass: HomeAssistant, selected_ids: list[str] | None = None
) -> list[AreaCandidate]:
    registry = ar.async_get(hass)
    selected = set(selected_ids or [])
    return [
        AreaCandidate(
            area_id=area.id,
            name=area.name,
            aliases=tuple(sorted(area.aliases)),
        )
        for area in registry.async_list_areas()
        if not selected or area.id in selected
    ]


def _preview_text(
    decisions: Mapping[str, Mapping[str, Any]], areas: list[AreaCandidate]
) -> tuple[str, int, int]:
    """Render the exact persisted decisions that setup will apply."""
    names = {area.area_id: area.name for area in areas}
    matched = [d for d in decisions.values() if d.get("area_id")]
    lines = [
        f"{d['zone_name']} → {names.get(d['area_id'], d['area_id'])} ({d['score']}%, {d['reason']})"
        for d in matched[:10]
    ]
    if len(matched) > 10:
        lines.append(f"…and {len(matched) - 10} more")
    return (
        "\n".join(lines) or "No high-confidence matches found.",
        len(matched),
        len(decisions) - len(matched),
    )


def _area_match_schema(
    selected_default: list[str], threshold_default: int, *, offer_rematch: bool
) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Optional(
            CONF_AREA_MATCH_AREAS, default=selected_default
        ): selector.AreaSelector(selector.AreaSelectorConfig(multiple=True)),
        vol.Required(CONF_AREA_MATCH_THRESHOLD, default=threshold_default): vol.All(
            vol.Coerce(int), vol.Range(min=60, max=100)
        ),
    }
    if offer_rematch:
        fields[vol.Required(CONF_REMATCH_EXISTING, default=False)] = bool
    return vol.Schema(fields)


def _delete_setup_repair_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    for suffix in (
        ISSUE_INVENTORY_ERROR,
        ISSUE_NO_PRIMARY_ZONES,
        ISSUE_SERIAL_MISMATCH,
    ):
        ir.async_delete_issue(hass, DOMAIN, f"{entry.entry_id}_{suffix}")


class DahuaArcConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 5

    def __init__(self) -> None:
        self._connection_data: dict[str, Any] = {}
        self._probe_result: dict[str, Any] = {}
        self._options: dict[str, Any] = {}
        self._preview: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = dict(user_input)
            user_input[CONF_HOST] = str(user_input[CONF_HOST]).strip()
            try:
                result = await _validate(self.hass, user_input)
            except Exception as exc:
                errors["base"] = _validation_error(exc)
            else:
                if not result.get("serial_number"):
                    errors["base"] = "serial_unavailable"
                    return self.async_show_form(
                        step_id="user", data_schema=_connection_schema(), errors=errors
                    )
                if any(
                    existing.data.get(CONF_ARC_SERIAL) == result["serial_number"]
                    for existing in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(str(result["serial_number"]))
                self._abort_if_unique_id_configured()
                self._async_abort_entries_match(
                    {
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_DHIP_PORT: user_input[CONF_DHIP_PORT],
                    }
                )
                self._connection_data = dict(user_input)
                self._connection_data[CONF_ARC_SERIAL] = str(result["serial_number"])
                self._probe_result = result
                return await self.async_step_behavior()

        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(), errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            updated = dict(entry.data)
            updated[CONF_USERNAME] = str(user_input[CONF_USERNAME])
            updated[CONF_PASSWORD] = str(user_input[CONF_PASSWORD])
            try:
                result = await _validate(self.hass, updated)
            except Exception as exc:
                errors["base"] = _validation_error(exc)
            else:
                if not _unique_id_matches_entry(entry, result, updated):
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        **updated,
                        CONF_ARC_SERIAL: str(result["serial_number"]),
                    },
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(entry.data),
            errors=errors,
            description_placeholders={"host": str(entry.data.get(CONF_HOST, ""))},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            user_input = dict(user_input)
            user_input[CONF_HOST] = str(user_input[CONF_HOST]).strip()
            # A blank password keeps the stored one.
            if not user_input.get(CONF_PASSWORD):
                user_input[CONF_PASSWORD] = entry.data[CONF_PASSWORD]
            try:
                result = await _validate(self.hass, user_input)
            except Exception as exc:
                errors["base"] = _validation_error(exc)
            else:
                if not _unique_id_matches_entry(entry, result, user_input):
                    return self.async_abort(reason="wrong_account")
                _delete_setup_repair_issues(self.hass, entry)
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        **user_input,
                        CONF_ARC_SERIAL: str(result["serial_number"]),
                    },
                    reason="reconfigure_successful",
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(entry.data, password_required=False),
            errors=errors,
        )

    async def async_step_behavior(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._options = dict(user_input)
            if self._options[CONF_AUTO_AREA_MATCH]:
                if not _area_candidates(self.hass):
                    errors["base"] = "no_areas"
                else:
                    return await self.async_step_area_match()
            else:
                return self._create_entry()

        return self.async_show_form(
            step_id="behavior",
            data_schema=_behavior_schema({}),
            errors=errors,
        )

    async def async_step_area_match(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        all_area_ids = [candidate.area_id for candidate in _area_candidates(self.hass)]
        if user_input is not None:
            self._options.update(user_input)
            selected_ids = self._options.get(CONF_AREA_MATCH_AREAS, [])
            candidates = _area_candidates(self.hass, selected_ids)
            items = list(self._probe_result.get("area_match_items", []))
            decisions = decide_zone_areas(
                items,
                candidates,
                threshold=int(self._options[CONF_AREA_MATCH_THRESHOLD]),
            )
            self._options[CONF_ZONE_AREA_DECISIONS] = decisions
            sample, matched, unmatched = _preview_text(decisions, candidates)
            self._preview = {
                "sample": sample,
                "matched": matched,
                "unmatched": unmatched,
                "total": len(decisions),
            }
            return await self.async_step_area_preview()

        return self.async_show_form(
            step_id="area_match",
            data_schema=_area_match_schema(
                all_area_ids, DEFAULT_AREA_MATCH_THRESHOLD, offer_rematch=False
            ),
        )

    async def async_step_area_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if user_input["confirm"]:
                return self._create_entry()
            return await self.async_step_area_match()

        return self.async_show_form(
            step_id="area_preview",
            data_schema=vol.Schema({vol.Required("confirm", default=True): bool}),
            description_placeholders={
                "matched": str(self._preview["matched"]),
                "unmatched": str(self._preview["unmatched"]),
                "total": str(self._preview["total"]),
                "sample": self._preview["sample"],
            },
        )

    def _create_entry(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"Dahua ARC {self._connection_data[CONF_HOST]}",
            data=self._connection_data,
            options=self._options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlowWithReload:
        return DahuaArcOptionsFlow()


class DahuaArcOptionsFlow(OptionsFlowWithReload):
    """Runtime options, including smart area matching."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._preview: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.config_entry.options
        if user_input is not None:
            self._options = dict(current)
            self._options.update(user_input)
            if self._options[CONF_AUTO_AREA_MATCH]:
                if not _area_candidates(self.hass):
                    return self.async_show_form(
                        step_id="init",
                        data_schema=_behavior_schema(user_input),
                        errors={"base": "no_areas"},
                    )
                return await self.async_step_area_match()
            return self.async_create_entry(data=self._options)

        suggested = {
            CONF_PERIODIC_RESYNC: current.get(
                CONF_PERIODIC_RESYNC, DEFAULT_PERIODIC_RESYNC
            ),
            CONF_AUTO_AREA_MATCH: current.get(
                CONF_AUTO_AREA_MATCH, DEFAULT_AUTO_AREA_MATCH
            ),
            CONF_ENABLE_RESEARCH_FEATURES: current.get(
                CONF_ENABLE_RESEARCH_FEATURES, DEFAULT_ENABLE_RESEARCH_FEATURES
            ),
        }
        return self.async_show_form(
            step_id="init", data_schema=_behavior_schema(suggested)
        )

    async def async_step_area_match(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.config_entry.options
        all_area_ids = [candidate.area_id for candidate in _area_candidates(self.hass)]
        if user_input is not None:
            user_input = dict(user_input)
            rematch = bool(user_input.pop(CONF_REMATCH_EXISTING, False))
            self._options.update(user_input)
            selected_ids = self._options.get(CONF_AREA_MATCH_AREAS, [])
            candidates = _area_candidates(self.hass, selected_ids)
            previous = dict(current.get(CONF_ZONE_AREA_DECISIONS, {}))
            decisions = decide_zone_areas(
                self._match_items(previous),
                candidates,
                threshold=int(self._options[CONF_AREA_MATCH_THRESHOLD]),
                # Stored decisions are kept unless the user explicitly asks to
                # re-evaluate them, so a rename never silently moves a zone.
                previous=None if rematch else previous,
            )
            self._options[CONF_ZONE_AREA_DECISIONS] = decisions
            sample, matched, unmatched = _preview_text(decisions, candidates)
            self._preview = {
                "sample": sample,
                "matched": matched,
                "unmatched": unmatched,
                "total": len(decisions),
            }
            return await self.async_step_area_preview()

        return self.async_show_form(
            step_id="area_match",
            data_schema=_area_match_schema(
                current.get(CONF_AREA_MATCH_AREAS, all_area_ids),
                current.get(CONF_AREA_MATCH_THRESHOLD, DEFAULT_AREA_MATCH_THRESHOLD),
                offer_rematch=bool(current.get(CONF_ZONE_AREA_DECISIONS)),
            ),
        )

    def _match_items(
        self, previous: Mapping[str, Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Zones to match: live topology, or stored decisions if not loaded."""
        hub = getattr(self.config_entry, "runtime_data", None)
        if hub is not None:
            return [
                {"index": zone.index, "name": zone.name, "area_hint": zone.area_hint}
                for zone in hub.primary_zones.values()
            ]
        # The entry is not running (e.g. ARC offline). Re-use the zone names
        # recorded with the previous decisions instead of wiping them.
        return [
            {"index": index, "name": decision.get("zone_name") or ""}
            for index, decision in previous.items()
        ]

    async def async_step_area_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if user_input["confirm"]:
                return self.async_create_entry(data=self._options)
            return await self.async_step_area_match()

        return self.async_show_form(
            step_id="area_preview",
            data_schema=vol.Schema({vol.Required("confirm", default=True): bool}),
            description_placeholders={
                "matched": str(self._preview["matched"]),
                "unmatched": str(self._preview["unmatched"]),
                "total": str(self._preview["total"]),
                "sample": self._preview["sample"],
            },
        )
