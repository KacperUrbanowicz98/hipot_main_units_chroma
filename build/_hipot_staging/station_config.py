"""Ustawienia stanowiska oraz stale prezentacyjne aplikacji."""

from __future__ import annotations

import os

APP_VERSION = "1.0.0"
APP_NAME = "Reconext Hi-Pot Main Units"
CONFIG_FILE = "station_config.json"


def steps_word(count: int) -> str:
    """Odmiana slowa "krok" - 1 krok, 2 kroki, 5 krokow, 12 krokow.

    Ekran pokazywal "1 kroków" i "3 kroków". Drobiazg, ale to napis, ktory
    operator czyta przy kazdej sztuce, a literowka w interfejsie podwaza
    zaufanie do liczb obok niej.
    """
    count = int(count)
    if count == 1:
        return "krok"
    if 12 <= count % 100 <= 14:
        return "kroków"
    return "kroki" if 2 <= count % 10 <= 4 else "kroków"


# ---------------------------------------------------------------------- #
# SKALOWANIE INTERFEJSU DO ROZDZIELCZOSCI EKRANU
# ---------------------------------------------------------------------- #
# Uklad ekranow byl rysowany na monitorze, ktory ma co najmniej tyle
# miejsca. Przy takim ekranie wspolczynnik wynosi 1.0 i aplikacja wyglada
# dokladnie tak, jak dotad - zero zmian dla stanowisk z duzym monitorem.
UI_DESIGN_SIZE = (1500, 900)
# Zapas na belke tytulu okna i pasek zadan - ten sam, co w panelu
# inzynieryjnym. Okno glowne jest maksymalizowane, wiec dostaje obszar
# roboczy, a nie caly ekran.
UI_SCREEN_MARGIN = (60, 90)
# Dolna granica. Ponizej tego napisy przestaja byc czytelne z odleglosci,
# z jakiej operator patrzy na stanowisko - lepiej zostawic pasek
# przewijania niz zrobic interfejs, ktorego nie da sie odczytac.
UI_MIN_FACTOR = 0.62
UI_MIN_FONT_PT = 7
# Ile razy wolno przebudowac ekran, dopasowujac skale do zmierzonej
# wysokosci. Trzy przebiegi wystarczaja na kazdej rozdzielczosci, ktora
# wystepuje na stanowiskach; czwarty jest zapasem.
UI_AUTOFIT_PASSES = 4
# Zapas przy korekcie. Bez niego ekran trafia dokladnie w krawedz i
# wystarczy jeden piksel obramowania wiecej, zeby znowu nie zmiescil sie.
UI_AUTOFIT_TRIM = 0.98


class UiScale:
    """Jeden wspolczynnik, przez ktory przechodza rozmiary interfejsu.

    Okno glowne jest maksymalizowane (``state("zoomed")``), wiec na kazdym
    monitorze dostaje tyle miejsca, ile system ma do oddania - ale TRESC
    miala rozmiary wpisane na sztywno, policzone dla duzego monitora.
    Ekran startowy potrzebuje 848 px wysokosci; na monitorze 1366x768 po
    odjeciu paska zadan zostaje ~678 px, wiec stopka i przycisk
    "URUCHOM TEST" lezaly POD krawedzia ekranu. Maksymalizacja tego nie
    naprawia - okno bylo duze, tylko tresc byla wieksza od okna.

    Wspolczynnik nigdy nie przekracza 1.0: na duzych monitorach interfejs
    ma zostac dokladnie taki, jaki jest dzis. Skalowanie dziala wylacznie
    w dol i wylacznie wtedy, gdy ekran jest mniejszy od projektowego.
    """

    def __init__(self) -> None:
        self._factor = 1.0

    # ------------------------------------------------------------------ #
    @property
    def factor(self) -> float:
        return self._factor

    def configure(self, screen_w: int, screen_h: int) -> float:
        """Ustawia wspolczynnik na podstawie rozmiaru ekranu."""
        usable_w = max(320, int(screen_w) - UI_SCREEN_MARGIN[0])
        usable_h = max(240, int(screen_h) - UI_SCREEN_MARGIN[1])
        factor = min(usable_w / UI_DESIGN_SIZE[0], usable_h / UI_DESIGN_SIZE[1])
        self._factor = max(UI_MIN_FACTOR, min(1.0, factor))
        return self._factor

    def configure_from(self, widget) -> float:
        """Jak ``configure``, ale rozmiar ekranu bierze z widgetu Tk."""
        return self.configure(widget.winfo_screenwidth(),
                              widget.winfo_screenheight())

    # ------------------------------------------------------------------ #
    def font(self, size: int, weight: str = "", family: str = "Arial"):
        """Krotka czcionki Tk po przeskalowaniu rozmiaru."""
        scaled = max(UI_MIN_FONT_PT, int(round(size * self._factor)))
        return (family, scaled, weight) if weight else (family, scaled)

    def px(self, value):
        """Przelicza wymiar w pikselach - liczbe albo krotke ``(a, b)``."""
        if isinstance(value, (tuple, list)):
            return tuple(self.px(item) for item in value)
        scaled = int(round(value * self._factor))
        # Zero zostaje zerem; wszystko inne ma zostac widoczne.
        return scaled if value <= 0 else max(1, scaled)

    # ------------------------------------------------------------------ #
    def available_height(self, widget) -> int:
        """Wysokosc, na ktora realnie mozna liczyc na tym ekranie."""
        try:
            screen_h = int(widget.winfo_screenheight())
        except Exception:
            return 0
        return max(240, screen_h - UI_SCREEN_MARGIN[1])

    def fits(self, widget) -> bool:
        """Czy zbudowany ekran miesci sie w pionie."""
        try:
            widget.update_idletasks()
            required = int(widget.winfo_reqheight())
        except Exception:
            return True
        available = self.available_height(widget)
        return required <= 0 or not available or required <= available

    def autofit(self, widget) -> bool:
        """Poprawia skale po zmierzeniu zbudowanego ekranu.

        Wspolczynnik policzony z samej rozdzielczosci to OSZACOWANIE.
        Czesc wysokosci nie zmienia sie razem z czcionka - obramowania,
        grubosci ramek, zaokraglenia wierszy tabeli - wiec ekran, ktory
        "powinien" zajac 678 px, zajmuje 752. Dlatego po zbudowaniu
        mierzymy, ile naprawde zajal, i poprawiamy. Zwraca ``True``,
        jesli ekran trzeba zbudowac jeszcze raz.

        Nie podnosi skali. Jesli ekran sie miesci, nie dzieje sie nic -
        stanowisko z duzym monitorem dostaje dokladnie to, co dotad.
        """
        try:
            widget.update_idletasks()
            required = int(widget.winfo_reqheight())
            screen_h = int(widget.winfo_screenheight())
        except Exception:
            # Pomiar nie moze przewrocic budowy ekranu operatora.
            return False
        available = max(240, screen_h - UI_SCREEN_MARGIN[1])
        if required <= 0 or required <= available:
            return False
        proposed = self._factor * (available / required) * UI_AUTOFIT_TRIM
        proposed = max(UI_MIN_FACTOR, min(1.0, proposed))
        # Ponizej dolnej granicy nie schodzimy nawet wtedy, gdy ekran
        # dalej nie wchodzi: nieczytelny interfejs jest gorszy niz
        # przyciety. Tak czy tak zglaszamy to do logu.
        if proposed >= self._factor - 0.005:
            if required > available:
                print(f"[UI] Ekran potrzebuje {required} px, jest "
                      f"{available} px, a skala jest juz na dolnej granicy "
                      f"({self._factor:.2f}).")
            return False
        self._factor = proposed
        return True

    def describe(self) -> str:
        return f"skala interfejsu {self._factor:.2f}"


# Jedna instancja na aplikacje. Dopoki nikt nie wywola configure(),
# wspolczynnik wynosi 1.0 - czyli zachowanie sprzed tej zmiany.
ui = UiScale()


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
    # Przerwa miedzy zgloszeniem CLOSED przez Arduino a wyslaniem SAFEty:STARt.
    # Aplikacja startowala ~20-50 ms po zamknieciu klapy. Krancowka jest juz
    # odfiltrowana z drgan w firmware (40 ms), wiec ta przerwa jest dodatkowym
    # marginesem dla mechaniki fixture - przydaje sie, gdy styk zwiera sie
    # zanim wyrob dosiadzie do konca w gniezdzie. Zakres 0-5 s.
    INTERLOCK_START_DELAY_S = 0.5

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
