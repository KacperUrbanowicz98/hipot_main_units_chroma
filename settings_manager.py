"""Trwala konfiguracja stanowiska i mapa HWID.

Rozdzial odpowiedzialnosci wzgledem aplikacji Amidala:

* ``station_config.json``  - USTAWIENIA STANOWISKA (porty, logi, haslo, model
  testera). Rozne na kazdym stanowisku, nie podlegaja walidacji technologicznej.
* ``products/*.json``      - PROFILE PRODUKTOW (napiecia, limity, kanaly).
  Identyczne na wszystkich stanowiskach, podlegaja zatwierdzeniu.
* ``hwid_map.json``        - przypisanie HWID -> (produkt, nazwa modelu).

Dzieki temu przeniesienie stanowiska na inny COM nie wymaga dotykania profilu
testowego, a zmiana profilu nie wymaga dotykania ustawien stanowiska.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from safety_rules import (
    SafetyValidationError,
    validate_interlock_settings,
    validate_rs232_settings,
)
from security import validate_password_record

STATION_CONFIG_FILE = "station_config.json"
HWID_MAP_FILE = "hwid_map.json"
SCHEMA_VERSION = 1


def atomic_write_json(path: str | os.PathLike[str], data: Any) -> None:
    """Zapisuje JSON atomowo - obserwator nigdy nie zobaczy polowy pliku."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def load_json_object(path: str | os.PathLike[str]) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        raise SafetyValidationError(f"Blad odczytu {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SafetyValidationError(f"{path} musi zawierac obiekt JSON")
    return data


class SettingsManager:

    # ------------------------------------------------------------------ #
    # MAPA HWID
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_hwid_entry(raw_hwid: Any, raw_entry: Any) -> tuple[str, dict[str, str]]:
        hwid = str(raw_hwid).strip().upper()
        if len(hwid) != 6 or not hwid.isascii() or not hwid.isalnum():
            raise SafetyValidationError(
                f"Nieprawidlowy HWID w {HWID_MAP_FILE}: {raw_hwid!r} "
                "(wymagane dokladnie 6 liter/cyfr)"
            )

        if isinstance(raw_entry, Mapping):
            product = str(raw_entry.get("product", "")).strip().upper()
            model = str(raw_entry.get("model", "")).strip()
        elif isinstance(raw_entry, str) and ":" in raw_entry:
            # Zapis skrocony "produkt:Model" dopuszczony dla wygody edycji reczej.
            product, _, model = raw_entry.partition(":")
            product = product.strip().upper()
            model = model.strip()
        else:
            raise SafetyValidationError(
                f"HWID {hwid}: wpis musi byc obiektem "
                '{"product": "...", "model": "..."} albo tekstem "produkt:Model"'
            )

        if not product:
            raise SafetyValidationError(f"HWID {hwid}: brak identyfikatora produktu")
        if not model:
            raise SafetyValidationError(f"HWID {hwid}: brak nazwy modelu")
        return hwid, {"product": product, "model": model}

    def load_hwid_map(self) -> dict[str, dict[str, str]]:
        if not os.path.exists(HWID_MAP_FILE):
            raise SafetyValidationError(
                f"Brak wymaganego pliku mapy HWID: {HWID_MAP_FILE}"
            )
        data = load_json_object(HWID_MAP_FILE)
        data.pop("schema_version", None)

        normalized: dict[str, dict[str, str]] = {}
        for raw_hwid, raw_entry in data.items():
            hwid, entry = self._normalize_hwid_entry(raw_hwid, raw_entry)
            normalized[hwid] = entry
        # Pusta mapa jest dopuszczalna: profile z serial.identify_by ==
        # "operator" (jak SR203_SR204) nie korzystaja z HWID. Profil
        # identyfikowany z HWID przy pustej mapie zostanie odrzucony przy
        # skanie z komunikatem "Nieznany HWID" - glosno, nie po cichu.
        return normalized

    def save_hwid_map(self, hwid_map: Mapping[str, Any]) -> None:
        normalized: dict[str, Any] = {}
        for raw_hwid, raw_entry in hwid_map.items():
            hwid, entry = self._normalize_hwid_entry(raw_hwid, raw_entry)
            normalized[hwid] = entry
        payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        payload.update(dict(sorted(normalized.items())))
        atomic_write_json(HWID_MAP_FILE, payload)

    # ------------------------------------------------------------------ #
    # KONFIGURACJA STANOWISKA
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_common(source: Mapping[str, Any], config) -> dict[str, Any]:
        from scpi_dialect import supported_models

        port, baudrate, parity, flow_control = validate_rs232_settings(
            source.get("DEVICE_COM_PORT", config.DEVICE_COM_PORT),
            source.get("DEVICE_BAUDRATE", config.DEVICE_BAUDRATE),
            source.get("DEVICE_PARITY", config.DEVICE_PARITY),
            source.get("DEVICE_FLOW_CONTROL", config.DEVICE_FLOW_CONTROL),
        )
        interlock_port, interlock_baudrate, interlock_enabled = (
            validate_interlock_settings(
                source.get("INTERLOCK_PORT", config.INTERLOCK_PORT),
                source.get("INTERLOCK_BAUDRATE", config.INTERLOCK_BAUDRATE),
                source.get("INTERLOCK_ENABLED", config.INTERLOCK_ENABLED),
            )
        )

        model = str(source.get("INSTRUMENT_MODEL", config.INSTRUMENT_MODEL)).strip().upper()
        if model not in supported_models():
            raise SafetyValidationError(
                f"INSTRUMENT_MODEL {model!r} nie jest obslugiwany. "
                f"Dostepne: {', '.join(supported_models())}"
            )

        log_dir = str(source.get("LOG_DIR", config.LOG_DIR) or "").strip()
        if not log_dir:
            raise SafetyValidationError("LOG_DIR nie moze byc pusty")

        auto_save = source.get("AUTO_SAVE_RESULTS", config.AUTO_SAVE_RESULTS)
        if not isinstance(auto_save, bool):
            raise SafetyValidationError("AUTO_SAVE_RESULTS musi byc true/false")
        if not auto_save:
            raise SafetyValidationError(
                "AUTO_SAVE_RESULTS musi pozostac true w wersji produkcyjnej"
            )

        station_id = str(source.get("STATION_ID", config.STATION_ID) or "").strip()
        if not station_id:
            raise SafetyValidationError(
                "STATION_ID nie moze byc pusty - identyfikuje stanowisko w raportach"
            )

        overrides = source.get("SCPI_OVERRIDES", config.SCPI_OVERRIDES) or {}
        if not isinstance(overrides, Mapping):
            raise SafetyValidationError("SCPI_OVERRIDES musi byc obiektem JSON")

        raw_enabled = source.get("ENABLED_PRODUCTS", config.ENABLED_PRODUCTS)
        if raw_enabled in (None, "", []):
            enabled_products = None
        elif isinstance(raw_enabled, (list, tuple)):
            enabled_products = sorted({
                str(item).strip().upper() for item in raw_enabled
                if str(item).strip()
            })
            if not enabled_products:
                raise SafetyValidationError(
                    "ENABLED_PRODUCTS nie moze byc lista samych pustych wpisow - "
                    "usun pole, aby wlaczyc wszystkie profile"
                )
        else:
            raise SafetyValidationError(
                "ENABLED_PRODUCTS musi byc lista identyfikatorow profili"
            )

        password_record = validate_password_record(
            source.get("ADMIN_PASSWORD", getattr(config, "ADMIN_PASSWORD", None))
        )

        return {
            "DEVICE_COM_PORT": port,
            "DEVICE_BAUDRATE": baudrate,
            "DEVICE_PARITY": parity,
            "DEVICE_FLOW_CONTROL": flow_control,
            "INTERLOCK_PORT": interlock_port,
            "INTERLOCK_BAUDRATE": interlock_baudrate,
            "INTERLOCK_ENABLED": interlock_enabled,
            "INSTRUMENT_MODEL": model,
            "STATION_ID": station_id,
            "LOG_DIR": log_dir,
            "AUTO_SAVE_RESULTS": auto_save,
            "ENABLED_PRODUCTS": enabled_products,
            "SCPI_OVERRIDES": dict(overrides),
            "ADMIN_PASSWORD": password_record,
        }

    def save_config(self, config) -> None:
        current = {
            key: getattr(config, key)
            for key in (
                "DEVICE_COM_PORT", "DEVICE_BAUDRATE", "DEVICE_PARITY",
                "DEVICE_FLOW_CONTROL", "INTERLOCK_PORT", "INTERLOCK_BAUDRATE",
                "INTERLOCK_ENABLED", "INSTRUMENT_MODEL", "STATION_ID",
                "LOG_DIR", "AUTO_SAVE_RESULTS", "ENABLED_PRODUCTS",
                "SCPI_OVERRIDES", "ADMIN_PASSWORD",
            )
        }
        validated = self._validate_common(current, config)

        payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        for key, value in validated.items():
            # None w tych polach znaczy "brak ograniczenia" / "brak rekordu" -
            # nie zapisujemy ich jako null, zeby plik zostal czytelny.
            if key in ("ADMIN_PASSWORD", "ENABLED_PRODUCTS") and value is None:
                continue
            payload[key] = value
        atomic_write_json(STATION_CONFIG_FILE, payload)

    def load_config(self, config) -> None:
        """Waliduje CALY plik przed zmiana obiektu config (transakcyjnie)."""
        if not os.path.exists(STATION_CONFIG_FILE):
            raise SafetyValidationError(
                f"Brak wymaganego pliku konfiguracji stanowiska: "
                f"{STATION_CONFIG_FILE}"
            )

        data = load_json_object(STATION_CONFIG_FILE)
        schema_version = data.get("schema_version", SCHEMA_VERSION)
        if not isinstance(schema_version, int) or schema_version > SCHEMA_VERSION:
            raise SafetyValidationError(
                f"{STATION_CONFIG_FILE} pochodzi z nowszej wersji aplikacji "
                f"(schema_version={schema_version!r})"
            )

        validated = self._validate_common(data, config)
        for key, value in validated.items():
            setattr(config, key, value)
        config.SCHEMA_VERSION = schema_version
