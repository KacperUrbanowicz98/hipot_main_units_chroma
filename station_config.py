"""Ustawienia stanowiska oraz stale prezentacyjne aplikacji."""

from __future__ import annotations

import os

APP_VERSION = "1.0.0"
APP_NAME = "Reconext Hi-Pot Main Units"
CONFIG_FILE = "station_config.json"


class StationConfig:
    """Konfiguracja JEDNEGO stanowiska. Profil produktu jest osobno."""

    APP_VERSION = APP_VERSION
    APP_NAME = APP_NAME
    WINDOW_TITLE = APP_NAME

    COLOR_BG = "#F5F5F5"
    COLOR_WHITE = "#FFFFFF"
    COLOR_PRIMARY = "#1A237E"
    # Kontrast wzgledem bieli / jasnego tla wg WCAG 2.1 (wymagane 4,5:1).
    # Poprzednie wartosci nie spelnialy progu: #4CAF50 dawal 2,78:1,
    # #F44336 3,68:1, a #FF9800 - kolor WSZYSTKICH instrukcji operacyjnych -
    # zaledwie 1,98:1. Operator odczytywal wynik wylacznie z koloru, bo napis
    # mial 11 pkt, a kolor byl za slaby.
    COLOR_ACCENT = "#2E7D32"        # biel na tym tle: 5,13:1
    COLOR_ERROR = "#C62828"         # biel na tym tle: 5,62:1
    COLOR_WARNING = "#B45309"       # na jasnym tle: 4,61:1
    # Stan normalny wymagajacy dzialania operatora - NIE jest awaria.
    # Wczesniej klapa otwarta (czyli kazda wymiana sztuki) byla pokazywana
    # tak samo jak awaria, wiec czerwien przestawala cokolwiek znaczyc.
    COLOR_ACTION = "#0D47A1"        # biel na tym tle: 9,26:1
    COLOR_ACTION_BG = "#E3F2FD"

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

    # Sciezka do manifestu sum kontrolnych profili. Puste = manifest lokalny
    # obok katalogu products (chroni przed edycja przypadkowa). Wskazanie
    # udzialu sieciowego TYLKO DO ODCZYTU zamienia to w realna kontrole -
    # patrz naglowek profile_integrity.py.
    PROFILE_MANIFEST_PATH = ""

    # Podpis plyty interlocka. Puste = bez sprawdzania. Wartosc musi byc
    # taka sama jak STATION_SIGNATURE w szkicu arduino/interlock/interlock.ino.
    INTERLOCK_IDENTITY = ""
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

    def __init__(self):
        self.SCPI_OVERRIDES = dict(type(self).SCPI_OVERRIDES)
        self.ADMIN_PASSWORD = None
        self.ENABLED_PRODUCTS = None
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
