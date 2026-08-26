"""Ustawienia stanowiska oraz stale prezentacyjne aplikacji."""

from __future__ import annotations

import os

APP_VERSION = "1.0.0"
APP_NAME = "Reconext Hi-Pot Main Units"
CONFIG_FILE = "station_config.json"
SCHEMA_VERSION = 1


class StationConfig:
    """Konfiguracja JEDNEGO stanowiska. Profil produktu jest osobno."""

    APP_VERSION = APP_VERSION
    APP_NAME = APP_NAME
    WINDOW_TITLE = APP_NAME

    COLOR_BG = "#F5F5F5"
    COLOR_WHITE = "#FFFFFF"
    COLOR_PRIMARY = "#1A237E"
    COLOR_ACCENT = "#4CAF50"
    COLOR_ERROR = "#F44336"
    COLOR_WARNING = "#FF9800"

    # --- Tester Hi-Pot -------------------------------------------------- #
    INSTRUMENT_MODEL = "19052"
    DEVICE_COM_PORT = "COM6"
    DEVICE_BAUDRATE = 19200
    DEVICE_PARITY = "NONE"
    DEVICE_FLOW_CONTROL = "NONE"

    # --- Interlock ------------------------------------------------------ #
    INTERLOCK_PORT = "COM5"
    INTERLOCK_BAUDRATE = 9600
    INTERLOCK_ENABLED = True

    # --- Raporty -------------------------------------------------------- #
    STATION_ID = "HIPOT-01"
    LOG_DIR = r"\\IFS\hipot_logs"
    AUTO_SAVE_RESULTS = True

    # --- Dostep --------------------------------------------------------- #
    # Skrot PBKDF2 hasla panelu inzynieryjnego. Dostarczany w
    # station_config.json - w kodzie nie ma hasla w postaci jawnej.
    ADMIN_PASSWORD = None

    # Profile aktywne na TYM stanowisku. Puste/None = wszystkie z katalogu.
    # Dzieki temu ta sama aplikacja moze stac na stanowisku testujacym tylko
    # SR203_SR204 i na stanowisku testujacym NR801, bez podmiany plikow
    # profili. Identyfikatory zapisujemy WIELKIMI literami (SR203_SR204).
    ENABLED_PRODUCTS = None

    # Nadpisania skladni SCPI - patrz scpi_dialect.Dialect._apply_overrides.
    SCPI_OVERRIDES: dict = {}

    def __init__(self, require_file: bool = True):
        self.SCPI_OVERRIDES = dict(type(self).SCPI_OVERRIDES)
        self.ADMIN_PASSWORD = None
        self.ENABLED_PRODUCTS = None
        if require_file:
            self._load()

    def _load(self) -> None:
        from safety_rules import SafetyValidationError
        from settings_manager import SettingsManager

        if not os.path.isfile(CONFIG_FILE):
            raise SafetyValidationError(
                f"Brak wymaganego pliku konfiguracji: {CONFIG_FILE}. "
                "Aplikacja nie uruchomi sie z ustawieniami domyslnymi, aby nie "
                "wykonac testu na przypadkowym porcie."
            )
        SettingsManager().load_config(self)

    # ------------------------------------------------------------------ #
    def dialect(self):
        from scpi_dialect import Dialect

        return Dialect(self.INSTRUMENT_MODEL, self.SCPI_OVERRIDES)

    def describe(self) -> str:
        """Opis stanowiska - uzywany w logu diagnostycznym, nie w interfejsie."""
        return (
            f"{self.STATION_ID} | Chroma {self.INSTRUMENT_MODEL} na "
            f"{self.DEVICE_COM_PORT} @{self.DEVICE_BAUDRATE} | "
            f"interlock {self.INTERLOCK_PORT}"
        )

    def is_product_enabled(self, product_id: str) -> bool:
        enabled = self.ENABLED_PRODUCTS
        if not enabled:
            return True
        return str(product_id or "").strip().upper() in enabled

    def footer_left(self) -> str:
        return f"{self.APP_NAME} v{self.APP_VERSION}"
