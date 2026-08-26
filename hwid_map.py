"""Mapa HWID -> (profil produktu, nazwa modelu).

Pierwsze 6 znakow numeru seryjnego wskazuje JEDNOCZESNIE profil testowy i nazwe
modelu drukowana w raporcie. Dzieki temu operator skanuje kod i aplikacja sama
wie, ktory profil uruchomic (SR203_SR204, NR801, SE210, ...) - bez przelacznika
trybu, ktory mozna ustawic zle.

Identyfikatory profili sa zapisywane WIELKIMI literami (``SR203_SR204``), tak
jak nazwy wyrobow. Porownania sa case-insensitive - recznie wpisane male litery
zostana znormalizowane przy wczytaniu.

Mapa jest w cache modulowym uniewaznianym po zmianie pliku (mtime + rozmiar).
Wersja 1.0.x czytala plik przy KAZDYM skanie, co przy mapie na udziale
sieciowym zawieszalo watek UI w krytycznym momencie cyklu.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from safety_rules import SafetyValidationError
from security import audit
from settings_manager import HWID_MAP_FILE, SettingsManager

_CACHE_LOCK = threading.Lock()
_CACHED_MAP: dict[str, dict[str, str]] | None = None
_CACHED_STAMP: tuple[float, int] | None = None


def _file_stamp() -> tuple[float, int] | None:
    try:
        info = os.stat(HWID_MAP_FILE)
    except OSError:
        return None
    return (info.st_mtime_ns / 1e9, info.st_size)


def invalidate_cache() -> None:
    global _CACHED_MAP, _CACHED_STAMP
    with _CACHE_LOCK:
        _CACHED_MAP = None
        _CACHED_STAMP = None


def _load_cached() -> dict[str, dict[str, str]]:
    global _CACHED_MAP, _CACHED_STAMP
    stamp = _file_stamp()
    with _CACHE_LOCK:
        if _CACHED_MAP is not None and stamp is not None and stamp == _CACHED_STAMP:
            return {key: dict(value) for key, value in _CACHED_MAP.items()}

    loaded = SettingsManager().load_hwid_map()
    with _CACHE_LOCK:
        _CACHED_MAP = {key: dict(value) for key, value in loaded.items()}
        _CACHED_STAMP = stamp
    return {key: dict(value) for key, value in loaded.items()}


class ScanResult:
    """Rozpoznany numer seryjny gotowy do uruchomienia testu."""

    __slots__ = ("serial", "hwid", "product_id", "model_name", "profile")

    def __init__(self, serial: str, hwid: str, product_id: str,
                 model_name: str, profile):
        self.serial = serial
        self.hwid = hwid
        self.product_id = product_id
        self.model_name = model_name
        self.profile = profile

    def __repr__(self) -> str:
        return (f"ScanResult(serial={self.serial!r}, product={self.product_id!r}, "
                f"model={self.model_name!r})")


class HwidMap:

    def __init__(self, catalog=None):
        self._map = _load_cached()
        self._catalog = catalog

    # ------------------------------------------------------------------ #
    def get_all(self) -> dict[str, dict[str, str]]:
        return {key: dict(value) for key, value in self._map.items()}

    def get_entry(self, serial: str) -> Optional[dict[str, str]]:
        if not serial or len(serial) < 6:
            return None
        return self._map.get(serial[:6].upper())

    def products_in_use(self) -> list[str]:
        return sorted({entry["product"] for entry in self._map.values()})

    def models_list(self) -> list[str]:
        return sorted({entry["model"] for entry in self._map.values()})

    # ------------------------------------------------------------------ #
    def resolve(self, serial: str) -> tuple[bool, object]:
        """Zwraca ``(True, ScanResult)`` albo ``(False, komunikat)``.

        Kolejnosc sprawdzen jest istotna: najpierw HWID (zeby poznac produkt),
        potem dlugosc S/N wedlug regul TEGO produktu. Rozne produkty moga miec
        rozne dopuszczalne dlugosci numeru.
        """
        raw = str(serial or "").strip().upper()
        if len(raw) < 6:
            return False, "Numer seryjny jest za krotki (minimum 6 znakow HWID)"

        entry = self._map.get(raw[:6])
        if entry is None:
            return False, f"Nieznany HWID '{raw[:6]}' - brak w mapie"

        if self._catalog is None:
            from product_profile import ProductCatalog
            self._catalog = ProductCatalog()

        try:
            profile = self._catalog.get(entry["product"])
        except SafetyValidationError as exc:
            return False, str(exc)

        try:
            normalized = profile.validate_serial(raw)
        except SafetyValidationError as exc:
            return False, str(exc)

        return True, ScanResult(
            serial=normalized,
            hwid=raw[:6],
            product_id=profile.product_id,
            model_name=entry["model"],
            profile=profile,
        )

    # ------------------------------------------------------------------ #
    def add(self, hwid: str, product_id: str, model: str) -> tuple[bool, str]:
        normalized_hwid = str(hwid or "").strip().upper()
        normalized_product = str(product_id or "").strip().upper()
        normalized_model = str(model or "").strip()

        if (len(normalized_hwid) != 6 or not normalized_hwid.isascii()
                or not normalized_hwid.isalnum()):
            return False, "HWID musi miec dokladnie 6 liter/cyfr"
        if not normalized_model:
            return False, "Nazwa modelu nie moze byc pusta"

        if self._catalog is None:
            from product_profile import ProductCatalog
            self._catalog = ProductCatalog()
        try:
            self._catalog.get(normalized_product)
        except SafetyValidationError as exc:
            return False, str(exc)

        previous = self._map.get(normalized_hwid)
        updated = self.get_all()
        updated[normalized_hwid] = {
            "product": normalized_product, "model": normalized_model
        }
        try:
            SettingsManager().save_hwid_map(updated)
        except Exception as exc:
            return False, f"Nie udalo sie zapisac mapy HWID: {exc}"

        invalidate_cache()
        self._map = updated
        audit("ZMIANA/HWID",
              f"{normalized_hwid}: {previous!r} -> "
              f"{{'product': {normalized_product!r}, 'model': {normalized_model!r}}}")
        return True, ""

    def remove(self, hwid: str) -> bool:
        normalized_hwid = str(hwid or "").strip().upper()
        if normalized_hwid not in self._map:
            return False
        previous = self._map[normalized_hwid]
        updated = self.get_all()
        del updated[normalized_hwid]
        SettingsManager().save_hwid_map(updated)
        invalidate_cache()
        self._map = updated
        audit("ZMIANA/HWID", f"{normalized_hwid}: {previous!r} -> USUNIETO")
        return True

def resolve_for_profile(profile, serial: str, catalog=None) -> tuple[bool, object]:
    """Rozpoznaje numer seryjny wedlug regul KONKRETNEGO profilu.

    JEDNO miejsce dla wszystkich ekranow. Poprzednio ekran startowy i okno
    "nastepny numer seryjny" mialy osobne implementacje i po dodaniu
    identyfikacji ``operator`` okno S/N nadal pytalo mape HWID - operator
    dostawal "Nieznany HWID" dla wyrobu, ktory mapy w ogole nie uzywa.

    Zwraca ``(True, ScanResult)`` albo ``(False, komunikat dla operatora)``.
    """
    if profile is not None and not profile.requires_hwid:
        try:
            normalized = profile.validate_serial(serial)
        except SafetyValidationError as exc:
            return False, str(exc)

        # Mapa jest opcjonalna, ale jesli opisuje ten prefiks INNYM profilem,
        # to znaczy, ze na stanowisku lezy niewlasciwy wyrob.
        try:
            entry = HwidMap(catalog).get_entry(normalized)
        except Exception:
            entry = None
        if entry and entry.get("product") != profile.product_id:
            return False, (
                f"HWID {normalized[:6]} jest przypisany do profilu "
                f"{entry.get('product')}, a stanowisko pracuje na "
                f"{profile.product_id}"
            )

        return True, ScanResult(
            serial=normalized,
            hwid=normalized[:6],
            product_id=profile.product_id,
            model_name=profile.display_name,
            profile=profile,
        )

    try:
        return HwidMap(catalog).resolve(serial)
    except Exception as exc:
        return False, f"Mapa HWID niedostepna: {exc}"
