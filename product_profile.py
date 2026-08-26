"""Profile produktow: definicja sekwencji testowej dla jednego wyrobu.

Profil jest DANYMI, nie kodem. Dodanie kolejnego produktu na Chromie sprowadza
sie do dodania pliku JSON w katalogu ``products`` i wpisow w mapie HWID -
bez rekompilacji EXE i bez ponownego przechodzenia walidacji oprogramowania.

Kazdy profil przechodzi pelna walidacje ``safety_rules`` przy wczytaniu.
Profil, ktory jej nie przejdzie, nie jest "pomijany" - blokuje uruchomienie
testu dla wszystkich przypisanych do niego HWID.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from safety_rules import (
    SafetyValidationError,
    channel_masks_overlap,
    profile_duration,
    step_duration,
    validate_serial,
    validate_step,
    validate_timeout_for_steps,
)

PRODUCTS_DIR_NAME = "products"
PROFILE_SCHEMA_VERSION = 1


class ProductProfile:
    """Zwalidowany, niemutowalny profil jednego produktu."""

    def __init__(self, data: Mapping[str, Any], source: str = "<pamiec>"):
        if not isinstance(data, Mapping):
            raise SafetyValidationError(f"{source}: profil musi byc obiektem JSON")
        self.source = source

        schema_version = data.get("schema_version", PROFILE_SCHEMA_VERSION)
        if not isinstance(schema_version, int) or schema_version > PROFILE_SCHEMA_VERSION:
            raise SafetyValidationError(
                f"{source}: profil w wersji schema_version={schema_version!r} "
                "jest nowszy niz obslugiwany przez aplikacje"
            )
        self.schema_version = schema_version

        self.product_id = str(data.get("product_id", "")).strip().upper()
        if not self.product_id or not self.product_id.replace("_", "").isalnum():
            raise SafetyValidationError(
                f"{source}: product_id musi zawierac litery, cyfry lub podkreslenia"
            )

        self.display_name = str(
            data.get("display_name", self.product_id)
        ).strip() or self.product_id

        instrument = data.get("instrument", {})
        if not isinstance(instrument, Mapping):
            raise SafetyValidationError(f"{source}: instrument musi byc obiektem JSON")

        raw_models = instrument.get("allowed_models", [])
        if isinstance(raw_models, str):
            raw_models = [raw_models]
        self.allowed_models = tuple(
            str(model).strip().upper() for model in raw_models if str(model).strip()
        )
        if not self.allowed_models:
            raise SafetyValidationError(
                f"{source}: instrument.allowed_models nie moze byc puste - profil "
                "musi jednoznacznie wskazywac model testera"
            )

        self.requires_scan_box = bool(instrument.get("requires_scan_box", False))
        self.channel_count = int(instrument.get("channel_count", 0) or 0)
        if self.requires_scan_box and self.channel_count <= 0:
            raise SafetyValidationError(
                f"{source}: profil wymaga scan boxa, ale nie podano channel_count"
            )
        if not self.requires_scan_box and self.channel_count:
            raise SafetyValidationError(
                f"{source}: channel_count ustawiony bez requires_scan_box"
            )

        serial_rules = data.get("serial", {})
        if not isinstance(serial_rules, Mapping):
            raise SafetyValidationError(f"{source}: serial musi byc obiektem JSON")
        raw_lengths = serial_rules.get("allowed_lengths", [14, 17])
        try:
            self.serial_lengths = tuple(sorted({int(x) for x in raw_lengths}))
        except (TypeError, ValueError) as exc:
            raise SafetyValidationError(
                f"{source}: serial.allowed_lengths musi byc lista liczb"
            ) from exc
        if not self.serial_lengths or any(
            not 4 <= length <= 64 for length in self.serial_lengths
        ):
            raise SafetyValidationError(
                f"{source}: serial.allowed_lengths poza zakresem 4-64"
            )

        # Skad aplikacja wie, ktory profil uruchomic dla zeskanowanej sztuki:
        #   "hwid"     - z mapy HWID (pierwsze 6 znakow S/N). Wyrob sam mowi,
        #                czym jest; operator nie moze tego nadpisac.
        #   "operator" - z listy wyboru na ekranie startowym. Uzywane tam,
        #                gdzie numery seryjne nie niosa informacji o wyrobie.
        #                Numer seryjny jest wtedy sprawdzany wylacznie pod
        #                katem dlugosci i zestawu znakow (wielkie litery
        #                A-Z i cyfry 0-9).
        identify_by = str(serial_rules.get("identify_by", "hwid")).strip().lower()
        if identify_by not in ("hwid", "operator"):
            raise SafetyValidationError(
                f"{source}: serial.identify_by musi byc 'hwid' albo 'operator', "
                f"jest {identify_by!r}"
            )
        self.identify_by = identify_by

        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
            raise SafetyValidationError(f"{source}: steps musi byc lista")
        if not raw_steps:
            raise SafetyValidationError(f"{source}: profil musi miec co najmniej 1 krok")

        steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            steps.append(validate_step(raw_step, index, self.channel_count))

        names = [step["name"] for step in steps]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise SafetyValidationError(
                f"{source}: nazwy krokow musza byc unikalne, powtorzone: {duplicates}"
            )
        self.steps: tuple[dict[str, Any], ...] = tuple(steps)

        self.channel_warnings: tuple[str, ...] = ()
        if self.channel_count:
            self.channel_warnings = tuple(
                channel_masks_overlap(
                    {step["name"]: step["channels"] for step in self.steps}
                )
            )

        self.test_timeout_s = validate_timeout_for_steps(
            data.get("test_timeout_s", 300), self.steps
        )

        self.notes = str(data.get("notes", "")).strip()
        self.revision = str(data.get("revision", "")).strip()
        # Nazwa programu drukowana w polu "Program:" raportu. Odpowiada nazwie
        # pliku .stp w oprogramowaniu Chromy, wiec bywa inna niz display_name.
        self.report_program = str(
            data.get("report_program", "") or self.display_name
        ).strip()

    # ------------------------------------------------------------------ #
    @property
    def requires_hwid(self) -> bool:
        """True, jesli profil jest rozpoznawany z mapy HWID."""
        return self.identify_by == "hwid"

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def total_duration(self) -> float:
        return profile_duration(self.steps)

    @staticmethod
    def effective_low_ma(step: Mapping[str, Any]) -> float:
        """Prog, ktory faktycznie trafia do Chromy jako Low Limit.

        Wyzszy z dwoch: Low Limit ze specyfikacji i progu obecnosci chroniacego
        przed testem pustego fixture.
        """
        return max(
            float(step["limit_low"]), float(step["presence_min_current"])
        )

    def step_duration(self, index: int) -> float:
        return step_duration(self.steps[index - 1])

    def matches_instrument(self, model: str) -> bool:
        return str(model or "").strip().upper() in self.allowed_models

    def validate_serial(self, serial: str) -> str:
        return validate_serial(serial, self.serial_lengths)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "product_id": self.product_id,
            "display_name": self.display_name,
            "instrument": {
                "allowed_models": list(self.allowed_models),
                "requires_scan_box": self.requires_scan_box,
            },
            "serial": {
                "allowed_lengths": list(self.serial_lengths),
                "identify_by": self.identify_by,
            },
            "test_timeout_s": self.test_timeout_s,
            "steps": [dict(step) for step in self.steps],
        }
        if self.channel_count:
            payload["instrument"]["channel_count"] = self.channel_count
        if self.report_program != self.display_name:
            payload["report_program"] = self.report_program
        if self.revision:
            payload["revision"] = self.revision
        if self.notes:
            payload["notes"] = self.notes
        return payload

    def summary_lines(self) -> list[str]:
        lines = [
            f"Produkt: {self.display_name} ({self.product_id})",
            "Identyfikacja: " + ("mapa HWID" if self.requires_hwid
                                 else "wybor operatora z listy")
            + f" | S/N: {'/'.join(str(x) for x in self.serial_lengths)} znakow",
            f"Tester: {', '.join(self.allowed_models)}"
            + (f", scan box {self.channel_count} kan." if self.channel_count else ""),
            f"Kroki: {self.step_count} | czas cyklu: {self.total_duration:.1f} s "
            f"| timeout: {self.test_timeout_s} s",
        ]
        for index, step in enumerate(self.steps, start=1):
            channels = f" | kanaly {step['channels']}" if step.get("channels") else ""
            lines.append(
                f"  {index}. {step['name']}: {step['voltage'] / 1000:.2f} kV, "
                f"{self.effective_low_ma(step):.3f}-{step['limit_high']:.3f} mA, "
                f"{step['ramp_time']:.1f}/{step['dwell']:.1f}/{step['ramp_dn']:.1f} s"
                f"{channels}"
            )
        return lines


# ---------------------------------------------------------------------- #
# KATALOG PROFILOW
# ---------------------------------------------------------------------- #
class ProductCatalog:
    """Wszystkie profile z katalogu ``products``."""

    def __init__(self, directory: str | os.PathLike[str] = PRODUCTS_DIR_NAME):
        self.directory = Path(directory)
        self._profiles: dict[str, ProductProfile] = {}
        self._errors: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self._profiles.clear()
        self._errors.clear()

        if not self.directory.is_dir():
            raise SafetyValidationError(
                f"Brak katalogu profilow produktow: {self.directory}"
            )

        files = sorted(self.directory.glob("*.json"))
        if not files:
            raise SafetyValidationError(
                f"Katalog {self.directory} nie zawiera zadnego profilu produktu"
            )

        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profile = ProductProfile(data, source=path.name)
            except Exception as exc:
                self._errors[path.name] = str(exc)
                continue

            if profile.product_id in self._profiles:
                self._errors[path.name] = (
                    f"product_id {profile.product_id!r} juz zdefiniowany w "
                    f"{self._profiles[profile.product_id].source}"
                )
                continue
            self._profiles[profile.product_id] = profile

        if not self._profiles:
            details = "; ".join(
                f"{name}: {error}" for name, error in self._errors.items()
            )
            raise SafetyValidationError(
                f"Zaden profil produktu nie przeszedl walidacji. {details}"
            )

    # ------------------------------------------------------------------ #
    @property
    def errors(self) -> dict[str, str]:
        """Pliki odrzucone przy wczytywaniu - MUSZA byc pokazane technologowi."""
        return dict(self._errors)

    def ids(self) -> list[str]:
        return sorted(self._profiles)

    def all(self) -> Iterable[ProductProfile]:
        return [self._profiles[key] for key in sorted(self._profiles)]

    def get(self, product_id: str) -> ProductProfile:
        key = str(product_id or "").strip().upper()
        try:
            return self._profiles[key]
        except KeyError as exc:
            known = ", ".join(sorted(self._profiles)) or "<brak>"
            raise SafetyValidationError(
                f"Nieznany profil produktu: {key!r}. Dostepne: {known}"
            ) from exc

    def save(self, profile: ProductProfile) -> Path:
        """Zapisuje profil atomowo pod ``products/<product_id>.json``."""
        from settings_manager import atomic_write_json

        path = self.directory / f"{profile.product_id}.json"
        atomic_write_json(path, profile.to_dict())
        self.reload()
        return path
