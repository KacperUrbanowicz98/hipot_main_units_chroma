"""Builder EXE silnika Hi-Pot Reconext.

Buduje aplikacje PyInstallerem w trybie ONEDIR bez podpisu cyfrowego.
Pliki ``station_config.json``, ``profiles_manifest.json`` i katalog
``products`` pozostaja obok EXE i moga byc edytowane z panelu
inzynieryjnego.

Kontrole przy budowaniu - i co kazda z nich RZECZYWISCIE daje:

1. Markery zabezpieczen. Kazdy plik zrodlowy musi zawierac wskazane elementy
   (nazwy funkcji i stalych stanowiacych bramki bezpieczenstwa) i nie moze
   zawierac elementow zabronionych ani tych usunietych swiadomie.
   Wykrywa: wyciecie calej bramki, powrot logowania operatora albo mapy HWID,
   jawne haslo zostawione w kodzie po debugowaniu.
   NIE wykrywa: podmiany TRESCI funkcji przy zachowanej nazwie. To kontrola
   pomylek, nie zabezpieczenie przed swiadoma zmiana.

2. Testy funkcjonalne w preflighcie. Uruchamiaja ``release_selftest.py``
   i sprawdzaja ZACHOWANIE, nie obecnosc napisow: ze zle i puste haslo
   faktycznie odpadaja, ze zaden profil nie ma kroku bez progu obecnosci,
   ze zadne pole tekstowe profilu nie zawiera znaku sterujacego.

3. ``approved_sources.json`` - sumy SHA-256 plikow zrodlowych.
   Gdy plik istnieje, kazda rozbieznosc zatrzymuje build.
   Daje: wykrycie przypadkowego zbudowania EXE ze starej albo roboczej kopii
   pliku, gdy nikt nie ruszal listy sum.
   NIE daje: dowodu integralnosci wydania. Lista lezy obok zrodel, regeneruje
   ja ``--approve``, nie jest podpisana i nie obejmuje ``products/*.json``
   ani ``station_config.json``. Nie opisuj jej w dokumentacji jakosciowej
   jako kontroli bezpieczenstwa. Wygenerujesz ja poleceniem:
       python create_exe.py --approve

Kontrola dzialajaca NA STANOWISKU to ``profiles_manifest.json`` - sumy
kontrolne profili sprawdzane przy kazdym uruchomieniu aplikacji.
Preflight generuje go razem z wydaniem i builder kopiuje obok EXE.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from station_config import APP_VERSION as SOURCE_APP_VERSION

APP_NAME = "Reconext Hi-Pot Main Units"
APP_DESCRIPTION = "Reconext Hi-Pot Main Units (Chroma 190xx)"
COMPANY_NAME = "Reconext"
VERSION = f"{SOURCE_APP_VERSION}.0"
COPYRIGHT = "Reconext 2026"

REQUIRED_PYTHON = (3, 13)
REQUIRED_PACKAGES = {
    "PyInstaller": ("pyinstaller", "6.21.0"),
    "serial": ("pyserial", "3.5"),
}

ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"
STAGING_DIR = BUILD_DIR / "_hipot_staging"
OUTPUT_DIR = DIST_DIR / APP_NAME
EXE_PATH = OUTPUT_DIR / f"{APP_NAME}.exe"
APPROVED_SOURCES_FILE = ROOT_DIR / "approved_sources.json"

PROJECT_FILES = [
    "main.py",
    "gui.py",
    "test_screen.py",
    "admin_panel.py",
    "station_config.py",
    "settings_manager.py",
    "product_profile.py",
    "scpi_dialect.py",
    "hipot_device.py",
    "interlock.py",
    "report_writer.py",
    "safety_rules.py",
    "security.py",
    "profile_integrity.py",
    "runtime_logging.py",
]

EDITABLE_DATA_FILES = [
    "station_config.json",
]

# Manifest sum kontrolnych profili. Generowany przez preflight i kopiowany
# obok EXE, zeby stanowisko startowalo ze ZWERYFIKOWANYM katalogiem profili.
# Bez niego aplikacja tworzy manifest lokalny przy pierwszym uruchomieniu -
# czyli zatwierdza to, co akurat lezy w products, i cala kontrola z buildu
# idzie w niwecz. Opcjonalny, bo przy PROFILE_MANIFEST_PATH wskazujacym
# udzial sieciowy manifestu na stanowisku nie ma wcale.
OPTIONAL_DATA_FILES = [
    "profiles_manifest.json",
]

PRODUCTS_DIR = "products"

HIDDEN_IMPORTS = [
    "tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog",
    "serial", "serial.tools.list_ports", "serial.tools.list_ports_windows",
    "hashlib", "hmac", "secrets",
] + [name[:-3] for name in PROJECT_FILES]


REQUIRED_SAFETY_MARKERS = {
    "safety_rules.py": (
        "ABSOLUTE_MIN_PRESENCE_MA = 0.010",
        "MIN_IN_RANGE_SAMPLES = 2",
        "def validate_step_pass_evidence",
        "def validate_cycle_pass_evidence",
        "def validate_channel_mask",
        # K3: pola tekstowe profilu nie moga dopisac wlasnej linii do raportu.
        "def validate_report_text",
    ),
    "hipot_device.py": (
        "_cycle_active_confirmed",
        "_pop_complete_line",
        "_verify_step_readback",
        "read_step_settings",
        "mask_to_channel_lists",
        "channels_high",
        "keylock_on",
        # K1: STOP przerywa trwajaca wymiane I/O, zamiast czekac na blokade
        # przez caly cykl ponowien (do ~3 s z napieciem na wyrobie).
        # Markery z wywolaniem, nie sama nazwa: "_abort_flag" jest prefiksem
        # "_abort_flag_off", wiec samo przemianowanie przeszloby kontrole.
        "self._abort_flag.set()",
        "self._abort_flag.is_set()",
        "def request_stop",
        # K2: zatrzymanie jest POTWIERDZANE odczytem z testera.
        "def confirm_stopped",
        "HV_OFF_VOLTAGE",
        # K6: bramki czasu liczone od znacznika zapisu SAFEty:STARt.
        "def cycle_started_monotonic",
        # S10: numer kroku z SET? porownywany z badanym krokiem.
        "_check_step_number",
    ),
    "test_screen.py": (
        "validate_step_pass_evidence",
        "validate_cycle_pass_evidence",
        "_valid_close_transition",
        "_cycle_terminal_seen",
        "fresh_cycle",
        # K1/K2: STOP z watku Tk i reakcja na brak potwierdzenia.
        "request_stop",
        "_show_stop_not_confirmed",
        # S3: pojedynczy transient nie uniewaznia PASS-a.
        "OVERCURRENT_STREAK_REQUIRED",
        # Przebieg bez wyniku zostawia slad w dzienniku audytowym.
        "_record_incomplete_run",
    ),
    "profile_integrity.py": (
        # K5: profil zmieniony poza panelem blokuje testowanie.
        "class ProfileIntegrity",
        "def verify_profiles",
        "def file_digest",
        "def save_manifest",
        "sha256",
    ),
    "report_writer.py": (
        "def save_report",
        # Zapis atomowy: watcher nigdy nie zobaczy polowy raportu.
        "os.replace",
        "fsync",
        # Dosylka raportow z katalogu awaryjnego na udzial.
        "def flush_pending_reports",
    ),
    "gui.py": (
        # Przebudowa ekranu nie moze siegac do zniszczonych widgetow.
        "_clear_screen_widgets",
        "_widget_alive",
        # Jedna droga sprawdzania numeru seryjnego dla wszystkich ekranow.
        "resolve_serial",
    ),
    "admin_panel.py": (
        # Kazda zmiana parametru testowego trafia do dziennika audytowego -
        # bez tego nie da sie odtworzyc, na jakich nastawach zrobiono partie.
        "audit_changes",
        # Zapis profilu produkcyjnego wymaga potwierdzenia, a okno pokazuje
        # WYLACZNIE zmieniane wartosci w formie "bylo -> bedzie".
        "_describe_step_changes",
        "askyesno",
        # Panel nie moze zapisac kroku, ktory nie przeszedl walidacji.
        "validate_step",
    ),
    "settings_manager.py": (
        # Twarde bramki konfiguracji: interlocka i zapisu raportow nie da sie
        # wylaczyc edycja JSON-a.
        "AUTO_SAVE_RESULTS musi pozostac true",
        "validate_password_record",
        # Zapis atomowy konfiguracji - zanik zasilania nie zostawi polowy.
        "def atomic_write_json",
    ),
    "scpi_dialect.py": (
        "PASS_JUDGMENT",
        "NON_TERMINAL_JUDGMENTS",
        # Kanaly scan boxa jako LISTY, zgodnie z manualem s. 5-21.
        "def encode_channel_list",
        "REQUIRED_SCAN_COMMANDS",
    ),
    "product_profile.py": (
        "validate_step",
        "validate_timeout_for_steps",
    ),
    "interlock.py": (
        "_extract_lines",
        "_heartbeat_expired",
    ),
    "security.py": (
        "pbkdf2_sha256",
        "hmac.compare_digest",
        "class AccessGate",
    ),
    "runtime_logging.py": (
        "configure_runtime_logging",
        "app_runtime_logs",
    ),
}

# Haslo standardowe stanowiska nie moze trafic do binarki w postaci jawnej -
# build jest niepodpisany, wiec literal dalby sie wyciagnac przez `strings`.
_PLAINTEXT_PASSWORD_MARKER = "recon" + "ext2026"
FORBIDDEN_MARKERS = {
    "gui.py": (_PLAINTEXT_PASSWORD_MARKER,),
    "admin_panel.py": (_PLAINTEXT_PASSWORD_MARKER,),
    "security.py": (_PLAINTEXT_PASSWORD_MARKER,),
    "station_config.py": (_PLAINTEXT_PASSWORD_MARKER,),
    # Logowanie operatora zostalo usuniete na zyczenie wlasciciela aplikacji.
}

# Elementy, ktore musza pozostac usuniete.
REMOVED_MARKERS = {
    "gui.py": ("_create_operator_panel", "_login_operator",
               "REQUIRE_OPERATOR_LOGIN"),
}

# Mapa HWID zostala usunieta w calosci: numery seryjne wyrobow testowanych
# na Chromie nie niosa informacji o modelu, wiec nie bylo z czego go
# odczytac. Profil wskazuje operator z listy. Gdyby ktos przywrocil czesc
# tego kodu, powstalaby DRUGA droga rozpoznawania wyrobu - a rozjechanie
# sie dwoch drog bylo zrodlem bledu zgloszonego ze stanowiska 26.08.
_HWID_MARKERS = ("HwidMap", "load_hwid_map", "requires_hwid", "identify_by")
for _module in ("gui.py", "test_screen.py", "product_profile.py",
                "settings_manager.py"):
    REMOVED_MARKERS[_module] = REMOVED_MARKERS.get(_module, ()) + _HWID_MARKERS


class BuildError(RuntimeError):
    pass


def print_header(title: str) -> None:
    print("\n" + "=" * 64)
    print(f"  {title}")
    print("=" * 64)


def run_command(command: list[str], cwd: Optional[Path] = None) -> None:
    print("[*] " + subprocess.list2cmdline(command))
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, text=True,
                            check=False)
    if result.returncode != 0:
        raise BuildError(
            f"Polecenie zakonczylo sie bledem {result.returncode}: {command[0]}")


def ensure_package(import_name: str, pip_name: str, expected: str) -> None:
    if importlib.util.find_spec(import_name) is None:
        raise BuildError(
            f"Brak pakietu {pip_name}=={expected}. Zainstaluj go w .venv: "
            f"{sys.executable} -m pip install {pip_name}=={expected}")
    try:
        actual = importlib.metadata.version(pip_name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise BuildError(f"Nie mozna odczytac wersji pakietu {pip_name}") from exc
    if actual != expected:
        raise BuildError(
            f"Wymagany {pip_name}=={expected}, znaleziono {actual}. "
            "Uzyj zatwierdzonego srodowiska .venv.")
    print(f"[+] {pip_name} {actual}: OK")


def verify_build_environment() -> None:
    if sys.version_info[:2] != REQUIRED_PYTHON:
        raise BuildError(
            f"Wymagany Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}.x, "
            f"uruchomiono {sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}")
    print(f"[+] Python {sys.version_info.major}.{sys.version_info.minor}."
          f"{sys.version_info.micro}: OK")
    for import_name, (pip_name, version) in REQUIRED_PACKAGES.items():
        ensure_package(import_name, pip_name, version)


def resolve_project_file(name: str, required: bool = True) -> Optional[Path]:
    """Uzywa WYLACZNIE pliku o dokladnej, kanonicznej nazwie.

    Celowo nie wybiera plikow typu ``test_screen(3).py`` - builder nie moze
    niejawnie zbudowac EXE ze starej lub przypadkowej kopii kodu.
    """
    exact = ROOT_DIR / name
    if exact.is_file():
        return exact
    if required:
        raise BuildError(
            f"Brak wymaganego pliku: {name}. Nadaj aktualnemu plikowi dokladnie "
            "te nazwe.")
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _string_constants(text: str) -> list[str]:
    """Wszystkie stale napisowe w pliku, po skleceniu prostych konkatenacji.

    Szukanie hasla jako PODCIAGU tekstu zrodlowego wykrywa tylko naiwne
    wklejenie. Parser Pythona sam skleja przylegajace literaly
    (``"recon" "ext2026"`` to jedna stala), a tu dodatkowo skladamy lancuchy
    ``"a" + "b"``. To zamyka realne przypadki; obfuskacji przez chr() albo
    base64 ta kontrola nie wykryje i nie udaje, ze wykrywa.
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    values: list[str] = []

    def fold(node) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    for node in ast.walk(tree):
        folded = fold(node)
        if folded:
            values.append(folded)
    return values


def validate_source_file(name: str, source: Path) -> None:
    text = source.read_text(encoding="utf-8", errors="strict")

    markers = FORBIDDEN_MARKERS.get(name, ())
    forbidden = [m for m in markers if m in text]
    if markers:
        constants = _string_constants(text)
        forbidden += [
            m for m in markers
            if m not in forbidden and any(m in value for value in constants)
        ]
    if forbidden:
        raise BuildError(
            f"{name}: zawiera elementy zabronione w buildzie produkcyjnym: "
            + ", ".join(sorted(set(forbidden))))

    returned = [m for m in REMOVED_MARKERS.get(name, ()) if m in text]
    if returned:
        raise BuildError(
            f"{name}: wrocily elementy, ktore zostaly USUNIETE swiadomie "
            "(logowanie operatora albo mapa HWID): " + ", ".join(returned))

    missing = [m for m in REQUIRED_SAFETY_MARKERS.get(name, ()) if m not in text]
    if missing:
        raise BuildError(
            f"{name}: brakuje wymaganych elementow zabezpieczen: "
            + ", ".join(missing))


def verify_approved_sources() -> None:
    """Porownuje sumy SHA-256 zrodel z lista zatwierdzona przez technologa."""
    if not APPROVED_SOURCES_FILE.is_file():
        print(f"[~] Brak {APPROVED_SOURCES_FILE.name} - pomijam kontrole sum "
              "kontrolnych. Wygeneruj: python create_exe.py --approve")
        return

    approved = json.loads(APPROVED_SOURCES_FILE.read_text(encoding="utf-8"))
    entries = approved.get("files", {})
    problems: list[str] = []
    for name in PROJECT_FILES:
        actual = sha256_file(ROOT_DIR / name)
        expected = entries.get(name)
        if expected is None:
            problems.append(f"{name}: brak na liscie zatwierdzonej")
        elif expected.upper() != actual:
            problems.append(f"{name}: suma {actual[:16]} != zatwierdzona "
                            f"{expected.upper()[:16]}")
    extra = sorted(set(entries) - set(PROJECT_FILES))
    if extra:
        problems.append(f"lista zawiera nieistniejace pliki: {extra}")

    if problems:
        raise BuildError(
            "Zrodla nie zgadzaja sie z zatwierdzona wersja:\n  - "
            + "\n  - ".join(problems)
            + "\n\nJesli zmiany sa celowe, zatwierdz je ponownie: "
              "python create_exe.py --approve")
    print(f"[+] Sumy SHA-256 zgodne z {APPROVED_SOURCES_FILE.name} "
          f"(zatwierdzone {approved.get('approved_at', '?')})")


def write_approved_sources() -> int:
    payload = {
        "approved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app_version": SOURCE_APP_VERSION,
        "files": {name: sha256_file(ROOT_DIR / name) for name in PROJECT_FILES},
    }
    APPROVED_SOURCES_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"[+] Zapisano {APPROVED_SOURCES_FILE.name} dla wersji "
          f"{SOURCE_APP_VERSION}")
    for name, digest in payload["files"].items():
        print(f"    {digest[:16]}  {name}")
    return 0


def run_release_preflight() -> None:
    print_header("Kontrola przed wydaniem")

    sources = [ROOT_DIR / name for name in PROJECT_FILES]
    sources += [ROOT_DIR / "release_selftest.py", ROOT_DIR / "create_exe.py"]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise BuildError("Brak plikow kontroli wydania: " + ", ".join(missing))

    run_command([sys.executable, "-m", "py_compile", *map(str, sources)],
                cwd=ROOT_DIR)
    print("[+] Kompilacja wszystkich skryptow: OK")

    run_command([sys.executable, str(ROOT_DIR / "release_selftest.py")],
                cwd=ROOT_DIR)

    validation = (
        "from station_config import StationConfig\n"
        "from product_profile import ProductCatalog\n"
        "cfg = StationConfig()\n"
        "assert cfg.INTERLOCK_ENABLED, 'INTERLOCK_ENABLED musi byc true'\n"
        "assert cfg.AUTO_SAVE_RESULTS, 'AUTO_SAVE_RESULTS musi byc true'\n"
        "assert cfg.ADMIN_PASSWORD, "
        "'station_config.json musi zawierac rekord ADMIN_PASSWORD'\n"
        "from security import verify_password\n"
        "assert verify_password('recon'+'ext2026', cfg.ADMIN_PASSWORD), "
        "'haslo standardowe stanowiska nie dziala'\n"
        # Kontrola ZACHOWANIA, nie obecnosci napisu: samo szukanie literalu
        # hasla wykrywa literowke, ale nie backdoora (sklejony literal
        # przechodzil). Tu sprawdzamy, ze zle haslo faktycznie odpada.
        "assert not verify_password('zle-haslo', cfg.ADMIN_PASSWORD), "
        "'weryfikacja hasla przepuszcza dowolna wartosc'\n"
        "assert not verify_password('', cfg.ADMIN_PASSWORD), "
        "'puste haslo przechodzi weryfikacje'\n"
        "catalog = ProductCatalog()\n"
        "assert not catalog.errors, f'Odrzucone profile: {catalog.errors}'\n"
        "assert catalog.ids(), 'Brak profili produktow'\n"
        # Zaden wlaczony profil nie moze miec kroku bez progu obecnosci -
        # bez niego pusty fixture zmierzylby zero i przeszedl jako PASS.
        "for profile in catalog.all():\n"
        "    for step in profile.steps:\n"
        "        assert step['presence_min_current'] > 0, profile.product_id\n"
        "enabled = [p for p in catalog.ids() if cfg.is_product_enabled(p)]\n"
        "assert enabled, 'Zaden profil nie jest wlaczony na stanowisku'\n"
        # K5: manifest sum profili jest generowany razem z wydaniem, zeby
        # stanowisko startowalo ze zweryfikowanym katalogiem.
        "from profile_integrity import ProfileIntegrity, save_manifest\n"
        "integrity = ProfileIntegrity(cfg, catalog.directory)\n"
        "if not integrity.external:\n"
        "    save_manifest(integrity.path, catalog.directory, "
        "note='manifest wygenerowany przy budowaniu wydania')\n"
        "integrity.check()\n"
        "assert not integrity.blocked, integrity.summary()\n"
        # K3: pola tekstowe profilu nie moga wstrzyknac linii do raportu.
        "for profile in catalog.all():\n"
        "    for text in [profile.report_program, profile.display_name] + "
        "[s['name'] for s in profile.steps]:\n"
        "        assert not set(text) & set('\\r\\n\\t'), "
        "f'znak sterujacy w polu raportu: {text!r}'\n"
        "print(f'[PREFLIGHT] Profile {catalog.ids()}, wlaczone {enabled}: OK')\n"
    )
    run_command([sys.executable, "-c", validation], cwd=ROOT_DIR)

    verify_approved_sources()
    print(f"[+] Wersja zrodla: {SOURCE_APP_VERSION}; wersja EXE: {VERSION}")


def create_version_file(path: Path) -> None:
    version_tuple = tuple(int(part) for part in VERSION.split("."))
    if len(version_tuple) != 4:
        raise BuildError("VERSION musi miec format np. 2.0.0.0")

    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '{COMPANY_NAME}'),
        StringStruct('FileDescription', '{APP_DESCRIPTION}'),
        StringStruct('FileVersion', '{VERSION}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('LegalCopyright', '{COPYRIGHT}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{COMPANY_NAME} {APP_NAME}'),
        StringStruct('ProductVersion', '{VERSION}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def create_runtime_hook(path: Path) -> None:
    path.write_text('''import os
import sys

if getattr(sys, "frozen", False):
    app_dir = os.path.dirname(os.path.abspath(sys.executable))
    internal_dir = getattr(sys, "_MEIPASS", os.path.join(app_dir, "_internal"))

    tcl_dir = os.path.join(internal_dir, "_tcl_data")
    tk_dir = os.path.join(internal_dir, "_tk_data")

    if os.path.isdir(tcl_dir):
        os.environ["TCL_LIBRARY"] = tcl_dir
    if os.path.isdir(tk_dir):
        os.environ["TK_LIBRARY"] = tk_dir

    os.chdir(app_dir)
''', encoding="utf-8")


def locate_tcl_tk() -> tuple[Path, Path]:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            tcl_dir = Path(root.tk.eval("info library")).resolve()
            tk_dir = Path(root.tk.eval("set tk_library")).resolve()
        finally:
            root.destroy()
    except Exception as exc:
        raise BuildError(f"Nie moge ustalic katalogow Tcl/Tk: {exc}") from exc

    required = [tcl_dir / "init.tcl", tk_dir / "tk.tcl",
                tk_dir / "ttk" / "scrollbar.tcl"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise BuildError("Instalacja Tcl/Tk jest niekompletna. Brakuje: "
                         + ", ".join(missing))
    print(f"[+] Tcl: {tcl_dir}")
    print(f"[+] Tk : {tk_dir}")
    return tcl_dir, tk_dir


def copy_tcl_tk_runtime(tcl_dir: Path, tk_dir: Path) -> None:
    print_header("Weryfikacja bibliotek Tcl/Tk")
    internal_dir = OUTPUT_DIR / "_internal"
    tcl_target = internal_dir / "_tcl_data"
    tk_target = internal_dir / "_tk_data"

    internal_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(tcl_dir, tcl_target, dirs_exist_ok=True)
    shutil.copytree(tk_dir, tk_target, dirs_exist_ok=True)

    required = [tcl_target / "init.tcl", tk_target / "tk.tcl",
                tk_target / "ttk" / "scrollbar.tcl"]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise BuildError("Po buildzie nadal brakuje plikow Tcl/Tk: "
                         + ", ".join(missing))
    print(f"[+] Tcl skopiowany do: {tcl_target}")
    print(f"[+] Tk/Ttk skopiowany do: {tk_target}")


def prepare_staging() -> dict[str, Path]:
    print_header("Przygotowanie plikow")
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    resolved: dict[str, Path] = {}
    for name in PROJECT_FILES:
        source = resolve_project_file(name)
        assert source is not None
        validate_source_file(name, source)
        shutil.copy2(source, STAGING_DIR / name)
        resolved[name] = source
        print(f"[+] {name}  SHA256:{sha256_file(source)[:16]}")

    for name in EDITABLE_DATA_FILES:
        source = resolve_project_file(name)
        assert source is not None
        resolved[name] = source
        print(f"[+] Dane edytowalne: {name}")

    for name in OPTIONAL_DATA_FILES:
        source = resolve_project_file(name, required=False)
        if source is None:
            print(f"[~] {name}: brak - manifest jest zewnetrzny albo "
                  "kontrola sum profili nie jest uzywana")
            continue
        resolved[name] = source
        print(f"[+] Dane edytowalne: {name}")

    products = ROOT_DIR / PRODUCTS_DIR
    if not products.is_dir() or not list(products.glob("*.json")):
        raise BuildError(
            f"Brak katalogu {PRODUCTS_DIR} albo nie zawiera profili produktow")
    print(f"[+] Profile produktow: "
          f"{[p.name for p in sorted(products.glob('*.json'))]}")

    create_version_file(STAGING_DIR / "version_info.txt")
    create_runtime_hook(STAGING_DIR / "runtime_hook_hipot.py")
    return resolved


def build_application(tcl_dir: Path, tk_dir: Path) -> None:
    print_header(f"Budowanie {APP_NAME}")
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    work_dir = BUILD_DIR / "pyinstaller"
    spec_dir = BUILD_DIR / "spec"
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--onedir", "--windowed", "--clean", "--noconfirm", "--noupx",
        "--name", APP_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(work_dir),
        "--specpath", str(spec_dir),
        "--version-file", str(STAGING_DIR / "version_info.txt"),
        "--runtime-hook", str(STAGING_DIR / "runtime_hook_hipot.py"),
        "--add-data", f"{tcl_dir};_tcl_data",
        "--add-data", f"{tk_dir};_tk_data",
    ]

    icon = resolve_project_file("hipot.ico", required=False)
    if icon:
        command.extend(["--icon", str(icon)])
        print(f"[+] Ikona: {icon.name}")
    else:
        print("[~] Brak hipot.ico - uzywam domyslnej ikony")

    for module in HIDDEN_IMPORTS:
        command.extend(["--hidden-import", module])

    command.append("main.py")
    run_command(command, cwd=STAGING_DIR)

    if not EXE_PATH.is_file():
        raise BuildError(f"Nie znaleziono utworzonego EXE: {EXE_PATH}")


def copy_editable_files(resolved: dict[str, Path]) -> None:
    print_header("Kopiowanie konfiguracji i profili")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for name in EDITABLE_DATA_FILES + OPTIONAL_DATA_FILES:
        source = resolved.get(name)
        if source:
            shutil.copy2(source, OUTPUT_DIR / name)
            print(f"[+] {name} obok EXE")

    target = OUTPUT_DIR / PRODUCTS_DIR
    shutil.copytree(ROOT_DIR / PRODUCTS_DIR, target, dirs_exist_ok=True)
    print(f"[+] {PRODUCTS_DIR}/ obok EXE "
          f"({len(list(target.glob('*.json')))} profili)")


def iter_output_files() -> Iterable[Path]:
    for path in sorted(OUTPUT_DIR.rglob("*")):
        if path.is_file() and path.name != "build_manifest.txt":
            yield path


def write_manifest() -> None:
    manifest = OUTPUT_DIR / "build_manifest.txt"
    lines = [
        f"Application: {APP_NAME}",
        f"Version: {VERSION}",
        f"Company: {COMPANY_NAME}",
        f"Build time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "Authenticode signed: NO",
        f"Approved sources file: "
        f"{'YES' if APPROVED_SOURCES_FILE.is_file() else 'NO'}",
        "",
        "SHA-256:",
    ]
    for path in iter_output_files():
        lines.append(f"{sha256_file(path)}  {path.relative_to(OUTPUT_DIR)}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def show_summary() -> None:
    exe_size = EXE_PATH.stat().st_size / (1024 * 1024)
    folder_size = sum(path.stat().st_size
                      for path in OUTPUT_DIR.rglob("*") if path.is_file())
    folder_size /= (1024 * 1024)

    print_header("BUILD ZAKONCZONY")
    print(f"Folder aplikacji : {OUTPUT_DIR}")
    print(f"Uruchamiaj       : {EXE_PATH}")
    print(f"Rozmiar EXE      : {exe_size:.1f} MB")
    print(f"Rozmiar folderu  : {folder_size:.1f} MB")
    print("Podpis cyfrowy   : NIE")
    print(f"\n[!] Kopiuj caly folder '{APP_NAME}', nie samo EXE.")
    print("[!] station_config.json, profiles_manifest.json i katalog "
          "products musza pozostac obok EXE.")
    print("[!] Haslo panelu inzynieryjnego jest opisane w dokumentacji "
          "wdrozeniowej; w plikach aplikacji jest tylko jego skrot.")
    print("[!] Ustaw uprawnienia NTFS: zapis do folderu tylko dla technologa.")


def main() -> int:
    try:
        if "--approve" in sys.argv:
            return write_approved_sources()

        if os.name != "nt":
            raise BuildError("Builder nalezy uruchomic na Windows.")

        verify_build_environment()
        run_release_preflight()
        resolved = prepare_staging()
        tcl_dir, tk_dir = locate_tcl_tk()
        build_application(tcl_dir, tk_dir)
        copy_tcl_tk_runtime(tcl_dir, tk_dir)
        copy_editable_files(resolved)
        write_manifest()
        show_summary()
        return 0

    except KeyboardInterrupt:
        print("\n[!] Anulowano przez uzytkownika.")
        return 130
    except BuildError as error:
        print_header("BLAD BUDOWANIA")
        print(f"[!] {error}")
        return 1
    except Exception as error:
        print_header("NIEOCZEKIWANY BLAD")
        print(f"[!] {type(error).__name__}: {error}")
        return 1
    finally:
        if sys.stdin.isatty():
            try:
                input("\nNacisnij Enter, aby zamknac...")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
