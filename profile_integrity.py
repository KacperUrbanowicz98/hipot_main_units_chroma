# profile_integrity.py
"""Kontrola sum kontrolnych profili produktow.

PROBLEM (K5 z audytu 31.08.2026)
--------------------------------
Profile ``products/*.json`` byly wczytywane z dysku bez zadnej kontroli
integralnosci. ``audit_changes`` wola sie WYLACZNIE z panelu inzynieryjnego,
wiec edycja pliku Notatnikiem omijala dziennik audytowy calkowicie.
Granice walidacji sa szerokie z koniecznosci (napiecie 100-5000 V, limit
gorny do 30 mA), wiec profil przerobiony na 100 V i limit 30 mA przechodzil
walidacje bez jednego ostrzezenia. Wyrob z izolacja przebijajaca przy 600 V
dostawal wtedy PASS, a cala warstwa dowodowa uznawala go za wiarygodny -
bo porownuje pomiar z profilem, a profil byl juz podmieniony.

JAK TO DZIALA
-------------
Manifest to plik JSON: ``{"<nazwa pliku>": "<SHA-256 tresci>"}``.
Przy starcie aplikacji liczymy sume kazdego profilu i porownujemy z wpisem.
Rozbieznosc, brak wpisu albo nadmiarowy plik BLOKUJE testowanie i trafia do
dziennika audytowego.

Panel inzynieryjny po zapisie profilu aktualizuje manifest i zapisuje zmiane
w dzienniku - edycja przez panel jest wiec legalna i zostawia slad, a edycja
pliku obok panelu zostaje wykryta.

CZEGO TO NIE DAJE - przeczytaj przed wpisaniem do dokumentacji jakosciowej
-------------------------------------------------------------------------
Manifest lezacy obok profili chroni przed edycja przypadkowa i przed edycja
"na szybko", ale nie przed kims, kto swiadomie podmieni oba pliki naraz.
Zostaje wtedy jeden slad: dziennik audytowy nie ma wpisu odpowiadajacego
zmianie sumy.

Zeby kontrola byla realna, ``PROFILE_MANIFEST_PATH`` w ``station_config.json``
powinien wskazywac manifest na udziale sieciowym, do ktorego stanowisko ma
prawo WYLACZNIE do odczytu. Wtedy podmiana profilu na stanowisku nie da sie
ukryc, bo manifestu nie da sie tam poprawic.

Przy pustym ``PROFILE_MANIFEST_PATH`` uzywany jest manifest lokalny.
Aplikacja informuje wtedy na ekranie startowym, ze profile nie sa objete
kontrola zewnetrzna - zeby nikt nie zalozyl ochrony, ktorej nie ma.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from safety_rules import SafetyValidationError
from settings_manager import atomic_write_json

MANIFEST_FILE_NAME = "profiles_manifest.json"
MANIFEST_SCHEMA_VERSION = 1


def file_digest(path: Path) -> str:
    """SHA-256 tresci pliku, zapisane wielkimi literami."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def default_manifest_path(products_dir: Path) -> Path:
    return Path(products_dir).parent / MANIFEST_FILE_NAME


def resolve_manifest_path(config, products_dir: Path) -> tuple[Path, bool]:
    """Zwraca ``(sciezka, czy_zewnetrzny)``.

    Zewnetrzny manifest (na udziale tylko do odczytu) jest jedyna wersja
    tej kontroli, ktora naprawde cos gwarantuje - patrz naglowek modulu.
    """
    configured = str(getattr(config, "PROFILE_MANIFEST_PATH", "") or "").strip()
    if configured:
        return Path(configured), True
    return default_manifest_path(products_dir), False


def build_manifest(products_dir: Path) -> dict[str, str]:
    products_dir = Path(products_dir)
    return {
        path.name: file_digest(path)
        for path in sorted(products_dir.glob("*.json"))
    }


def load_manifest(path: Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        raise SafetyValidationError(f"Brak manifestu profili: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyValidationError(
            f"Nie udalo sie odczytac manifestu {path}: {exc}"
        ) from exc
    if not isinstance(data, Mapping):
        raise SafetyValidationError(f"{path}: manifest musi byc obiektem JSON")

    version = data.get("schema_version", MANIFEST_SCHEMA_VERSION)
    if not isinstance(version, int) or version > MANIFEST_SCHEMA_VERSION:
        raise SafetyValidationError(
            f"{path}: manifest w wersji {version!r} jest nowszy niz aplikacja"
        )

    entries = data.get("profiles", {})
    if not isinstance(entries, Mapping):
        raise SafetyValidationError(f"{path}: pole 'profiles' musi byc obiektem")
    return {
        str(name): str(digest).strip().upper()
        for name, digest in entries.items()
    }


def save_manifest(path: Path, products_dir: Path,
                  note: str = "") -> dict[str, str]:
    """Zapisuje manifest dla biezacej zawartosci katalogu profili."""
    profiles = build_manifest(products_dir)
    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "profiles": dict(sorted(profiles.items())),
    }
    if note:
        payload["note"] = str(note)
    atomic_write_json(str(path), payload)
    return profiles


def verify_profiles(products_dir: Path,
                    manifest_path: Path) -> list[str]:
    """Zwraca liste rozbieznosci. Pusta lista = profile nienaruszone."""
    products_dir = Path(products_dir)
    expected = load_manifest(manifest_path)
    actual = build_manifest(products_dir)

    problems: list[str] = []
    for name in sorted(set(expected) | set(actual)):
        if name not in actual:
            problems.append(f"{name}: plik profilu zniknal z katalogu")
        elif name not in expected:
            problems.append(f"{name}: profil spoza manifestu (dopisany plik)")
        elif expected[name] != actual[name]:
            problems.append(
                f"{name}: tresc zmieniona poza panelem "
                f"(oczekiwano {expected[name][:16]}..., "
                f"jest {actual[name][:16]}...)"
            )
    return problems


class ProfileIntegrity:
    """Stan kontroli integralnosci dla jednej sesji aplikacji."""

    def __init__(self, config, products_dir: Path):
        self.products_dir = Path(products_dir)
        self.path, self.external = resolve_manifest_path(config,
                                                         self.products_dir)
        self.problems: list[str] = []
        self.error: Optional[str] = None

    @property
    def blocked(self) -> bool:
        """True, gdy testowanie musi zostac zablokowane."""
        return bool(self.problems) or self.error is not None

    def check(self, *, create_if_missing: bool = True) -> None:
        """Sprawdza profile. Przy braku LOKALNEGO manifestu tworzy go.

        Brak manifestu ZEWNETRZNEGO jest bledem konfiguracji - stanowisko ma
        go tylko czytac, wiec jego brak oznacza albo zla sciezke, albo
        niedostepny udzial, i w obu przypadkach nie wolno testowac.
        """
        self.problems = []
        self.error = None
        try:
            if not self.path.is_file():
                if self.external or not create_if_missing:
                    self.error = (
                        f"Brak manifestu profili: {self.path}. "
                        "Sprawdz dostepnosc udzialu i sciezke "
                        "PROFILE_MANIFEST_PATH."
                    )
                    return
                save_manifest(self.path, self.products_dir,
                              note="manifest utworzony automatycznie "
                                   "przy pierwszym uruchomieniu")
                print(f"[PROFILE] Utworzono manifest lokalny: {self.path}")
                return
            self.problems = verify_profiles(self.products_dir, self.path)
        except SafetyValidationError as exc:
            self.error = str(exc)
        except Exception as exc:  # pragma: no cover - awaria dysku/udzialu
            self.error = f"Nie udalo sie sprawdzic profili: {exc}"

        if self.blocked:
            from security import audit

            detail = self.error or "; ".join(self.problems)
            print(f"[PROFILE] KONTROLA SUM NIEZGODNA: {detail}")
            try:
                audit("PROFILE/INTEGRALNOSC", detail)
            except Exception as audit_error:
                print(f"[PROFILE] Nie zapisano do dziennika: {audit_error}")

    def refresh_after_panel_edit(self, product_id: str) -> Optional[str]:
        """Po legalnej zmianie profilu z panelu manifest musi sie zgadzac.

        Manifest zewnetrzny lezy na udziale tylko do odczytu, wiec tej drogi
        nie ma - zmiane profilu trzeba wtedy zatwierdzic u technologa
        i rozeslac nowy manifest. Zwraca komunikat do pokazania albo ``None``.
        """
        from security import audit

        if self.external:
            audit("PROFILE/INTEGRALNOSC",
                  f"{product_id}: zmiana profilu przy manifescie zewnetrznym "
                  f"({self.path}) - wymagane zatwierdzenie i nowy manifest")
            return (
                "Profil zapisany, ale manifest jest zewnętrzny "
                f"({self.path}) i nie został zaktualizowany.\n"
                "Do czasu rozesłania nowego manifestu to stanowisko "
                "zablokuje testowanie przy następnym uruchomieniu."
            )
        try:
            save_manifest(self.path, self.products_dir,
                          note=f"aktualizacja po zapisie profilu {product_id}")
        except Exception as exc:
            return f"Nie udało się zaktualizować manifestu profili: {exc}"
        audit("PROFILE/INTEGRALNOSC",
              f"{product_id}: manifest lokalny zaktualizowany po zapisie")
        self.problems = []
        self.error = None
        return None

    def summary(self) -> str:
        if self.error:
            return f"⛔ Kontrola profili: {self.error}"
        if self.problems:
            return ("⛔ Profile zmienione poza panelem inżynieryjnym: "
                    + "; ".join(self.problems))
        if self.external:
            return f"✓ Profile zgodne z manifestem {self.path}"
        return ("⚠ Profile zgodne z manifestem lokalnym — bez kontroli "
                "zewnętrznej (ustaw PROFILE_MANIFEST_PATH)")
