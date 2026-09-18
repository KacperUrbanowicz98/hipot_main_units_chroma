"""Okno glowne aplikacji: skanowanie numeru seryjnego.

Profil testowy wybiera operator z listy rozwijanej; wybrany profil jest
pokazany stale, razem z napieciami i kanalami, ktore z niego wynikaja.
Numer seryjny jest sprawdzany co do dlugosci
zapisanej w profilu (dla wszystkich obecnych wyrobow: 14 znakow) i zestawu
znakow - wylacznie wielkie litery A-Z i cyfry 0-9. Male litery sa
podnoszone juz przy wpisywaniu.

Nie ma mapy HWID: numery seryjne wyrobow testowanych na Chromie nie niosa
informacji o modelu, wiec nie bylo z czego go odczytac.

Profile moga byc wlaczane i wylaczane per stanowisko (``ENABLED_PRODUCTS``).
Wylaczony profil nie pojawia sie na liscie, wiec nie da sie go uruchomic.
Przy wiecej niz jednym wlaczonym profilu lista startuje PUSTA - operator musi
wskazac wyrob swiadomie, a wybrany profil jest widoczny przez caly czas pracy
razem z napieciami i kanalami, ktore z niego wynikaja.
"""

from __future__ import annotations

import tkinter as tk
import traceback
from tkinter import messagebox, ttk
from typing import Any

from admin_panel import AdminPanel
from product_profile import ProductCatalog
from security import AccessGate, audit, verify_password
from station_config import StationConfig


class HiPotApp:

    def __init__(self, root):
        self.root = root
        self.config = StationConfig()
        self.catalog = ProductCatalog()
        # K5: profile moga byc zmienione poza panelem. Kontrola sum jest
        # robiona RAZ przy starcie i blokuje testowanie, jesli tresc profilu
        # nie zgadza sie z manifestem.
        from profile_integrity import ProfileIntegrity

        self.integrity = ProfileIntegrity(self.config, self.catalog.directory)
        try:
            self.integrity.check()
        except Exception as exc:
            print(f"[PROFILE] Błąd kontroli integralności: {exc}")
            traceback.print_exc()
            self.integrity.error = str(exc)
        self.current_test_screen = None

        self._scan_pending = False
        self._scan_after_id = None
        self._admin_gate = AccessGate()
        self._shortcut_presses = 0
        self._shortcut_timer = None

        self._setup_window()
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self._build_screen()

    def _setup_window(self) -> None:
        self.root.title(self.config.WINDOW_TITLE)
        try:
            # "zoomed" istnieje tylko w Windows; brak obslugi nie moze
            # przewrocic startu aplikacji.
            self.root.state("zoomed")
        except tk.TclError:
            self.root.attributes("-fullscreen", False)
        self.root.configure(bg=self.config.COLOR_BG)

    def _on_closing(self) -> None:
        screen = self.current_test_screen
        busy = bool(screen is not None
                    and (screen.test_running
                         or getattr(screen, "_result_pending", False)))
        if busy and not messagebox.askyesno(
            "Cykl Hi-Pot w toku",
            "Zamknięcie aplikacji zatrzyma aktywny test lub przerwie "
            "finalizację wyniku. Zamknąć aplikację?",
            parent=self.root,
        ):
            return
        if screen is not None:
            screen.shutdown()
        self.root.destroy()

    # ------------------------------------------------------------------ #
    # BUDOWA EKRANU
    # ------------------------------------------------------------------ #
    def _build_screen(self) -> None:
        # Przebudowa ekranu startowego nigdy nie zachodzi przy podanym
        # wysokim napieciu, wiec jej blad NIE moze zamykac aplikacji przez
        # fatalny handler Tk. Operator ma dostac czytelny komunikat i moc
        # wrocic do pracy.
        try:
            self._build_screen_unsafe()
        except Exception as exc:
            print(f"[GUI] Blad budowy ekranu startowego: {exc!r}")
            traceback.print_exc()
            self._show_screen_build_error(exc)

    def _show_screen_build_error(self, exc: Exception) -> None:
        for widget in self.root.winfo_children():
            try:
                widget.destroy()
            except tk.TclError:
                pass
        self._clear_screen_widgets()
        frame = tk.Frame(self.root, bg=self.config.COLOR_ERROR)
        frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(frame, text="⛔ Błąd budowy ekranu",
                 bg=self.config.COLOR_ERROR, fg=self.config.COLOR_WHITE,
                 font=("Arial", 24, "bold")).pack(pady=(60, 10))
        tk.Label(frame, text=f"{exc}\n\nZgłoś błąd.",
                 bg=self.config.COLOR_ERROR, fg="#FFEBEE",
                 font=("Arial", 12), justify="center",
                 wraplength=900).pack(pady=(0, 20), padx=30)
        tk.Button(frame, text="Spróbuj ponownie", bg=self.config.COLOR_WHITE,
                  fg=self.config.COLOR_ERROR, font=("Arial", 13, "bold"),
                  relief=tk.FLAT, cursor="hand2", padx=20, pady=8,
                  command=self.show_scan_screen).pack()

    def _build_screen_unsafe(self) -> None:
        self._create_header()
        self.main_frame = tk.Frame(self.root, bg=self.config.COLOR_BG)
        self.main_frame.pack(expand=True, fill=tk.BOTH, padx=20, pady=(16, 50))

        self._create_integrity_banner(self.main_frame)
        self._create_scan_panel(self.main_frame)
        self._create_catalog_warnings(self.main_frame)
        self._create_footer()
        self._bind_admin_shortcut()

    # Widgety ekranu startowego. Po przebudowie ekranu (powrot z testu,
    # zamkniecie panelu) STARE obiekty sa zniszczone, ale atrybuty nadal na
    # nie wskazuja - kazde ``label.config(...)`` na takim atrybucie konczy sie
    # TclError "invalid command name". Lista sluzy do wyzerowania referencji
    # PRZED budowa nowego ekranu.
    _SCREEN_WIDGETS = (
        "serial_entry", "serial_hint_label", "scan_status_label", "confirm_btn",
        "profile_combo", "profile_banner", "profile_banner_title",
        "profile_info_label",
    )

    def _clear_screen_widgets(self) -> None:
        for name in self._SCREEN_WIDGETS:
            setattr(self, name, None)

    @staticmethod
    def _widget_alive(widget) -> bool:
        """Czy widget nadal istnieje po stronie Tk."""
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def show_scan_screen(self) -> None:
        self.current_test_screen = None
        self._scan_pending = False
        if self._scan_after_id:
            try:
                self.root.after_cancel(self._scan_after_id)
            except Exception as exc:
                print(f"[GUI] {exc!r}")
            self._scan_after_id = None
        for widget in self.root.winfo_children():
            widget.destroy()
        self._clear_screen_widgets()
        self._build_screen()

    def _create_header(self) -> None:
        header = tk.Frame(self.root, bg=self.config.COLOR_PRIMARY, height=68)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text=self.config.WINDOW_TITLE,
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 21, "bold")).pack(side=tk.LEFT, padx=20, pady=14)
        # ID stanowiska musi byc widoczne: przy audycie i przy zgloszeniu
        # awarii to pierwsza informacja, o ktora ktos zapyta.
        tk.Label(header, text=f"Stanowisko: {self.config.STATION_ID}",
                 bg=self.config.COLOR_PRIMARY, fg="#C5CAE9",
                 font=("Arial", 13, "bold")).pack(side=tk.RIGHT, padx=20)

    def _create_footer(self) -> None:
        footer = tk.Frame(self.root, bg=self.config.COLOR_PRIMARY, height=38)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        footer.pack_propagate(False)
        tk.Label(footer, text=self.config.footer_left(),
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=20, pady=9)
        tk.Label(footer, text="Autor: Kacper Urbanowicz",
                 bg=self.config.COLOR_PRIMARY, fg=self.config.COLOR_WHITE,
                 font=("Arial", 10, "bold")).pack(side=tk.RIGHT, padx=20, pady=9)

    def _create_integrity_banner(self, parent) -> None:
        """Stan kontroli sum profili - widoczny, nie schowany w logu."""
        if self.integrity.blocked:
            frame = tk.Frame(parent, bg=self.config.COLOR_ERROR)
            frame.pack(fill=tk.X, pady=(0, 8))
            tk.Label(frame,
                     text="⛔ TESTOWANIE ZABLOKOWANE — profile nie zgadzają się "
                          "z manifestem",
                     bg=self.config.COLOR_ERROR, fg=self.config.COLOR_WHITE,
                     font=("Arial", 15, "bold")).pack(pady=(8, 2))
            detail = self.integrity.error or "; ".join(self.integrity.problems)
            tk.Label(frame, text=f"{detail}\n\nZgłoś błąd.",
                     bg=self.config.COLOR_ERROR, fg="#FFEBEE",
                     font=("Arial", 10), justify="center",
                     wraplength=1000).pack(pady=(0, 8), padx=20)
            return
        # Informacja o RODZAJU manifestu (lokalny kontra zewnetrzny) jest
        # dla technologa, nie dla operatora - operator nie ma jak na nia
        # zareagowac, a staly komunikat ostrzegawczy, ktorego nie da sie
        # obsluzyc, uczy ignorowania ostrzezen. Stan kontroli sum jest
        # pokazany w panelu inzynieryjnym, zakladka Profile.

    def _create_catalog_warnings(self, parent) -> None:
        """Odrzucone profile MUSZA byc widoczne, a nie cicho pominiete."""
        if not self.catalog.errors:
            return
        frame = tk.Frame(parent, bg="#ffebee", relief=tk.RAISED, borderwidth=2)
        frame.pack(fill=tk.X, pady=(10, 0))
        tk.Label(frame, text="⚠ Profile produktów odrzucone przy wczytaniu",
                 bg="#ffebee", fg=self.config.COLOR_ERROR,
                 font=("Arial", 10, "bold")).pack(anchor="w", padx=12, pady=(6, 2))
        for name, error in sorted(self.catalog.errors.items()):
            tk.Label(frame, text=f"• {name}: {error}", bg="#ffebee", fg="#b71c1c",
                     font=("Arial", 9), justify="left", wraplength=900).pack(
                         anchor="w", padx=22, pady=1)
        tk.Label(frame,
                 text="Wyroby przypisane do tych profili nie zostaną "
                      "przetestowane do czasu poprawienia plików.",
                 bg="#ffebee", fg="#b71c1c", font=("Arial", 8, "italic")).pack(
                     anchor="w", padx=12, pady=(2, 6))

    # ------------------------------------------------------------------ #
    # SKANOWANIE S/N
    # ------------------------------------------------------------------ #
    def _create_scan_panel(self, parent) -> None:
        center = tk.Frame(parent, bg=self.config.COLOR_BG)
        center.pack(expand=True)
        panel = tk.Frame(center, bg=self.config.COLOR_WHITE,
                         relief=tk.RAISED, borderwidth=2)
        panel.pack(padx=50, pady=30)

        # Krok 1 - wybor profilu.
        tk.Label(panel, text="1.  Wybierz profil testowy",
                 bg=self.config.COLOR_WHITE, fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 17, "bold")).pack(pady=(24, 8), padx=40,
                                                  anchor="w")
        self._create_profile_selector(panel)

        # Krok 2 - numer seryjny.
        tk.Label(panel, text="2.  Zeskanuj numer seryjny",
                 bg=self.config.COLOR_WHITE, fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 17, "bold")).pack(pady=(18, 6), padx=40,
                                                  anchor="w")

        # Numer seryjny jest ZAWSZE wielkimi literami. Podnoszenie w locie,
        # a nie dopiero przy walidacji, zeby operator widzial na ekranie
        # dokladnie to, co trafi do raportu i do nazwy pliku.
        self._uppercase_pending = False
        self._serial_var = tk.StringVar()
        self._serial_var.trace_add("write", self._force_uppercase_serial)
        self.serial_entry = tk.Entry(panel, font=("Consolas", 22, "bold"),
                                     width=22, justify="center",
                                     relief=tk.SOLID, borderwidth=2,
                                     textvariable=self._serial_var)
        self.serial_entry.pack(pady=(0, 4), padx=50, ipady=6)
        self.serial_entry.focus()
        self.serial_entry.bind("<Return>", lambda event: self._process_serial())

        self.serial_hint_label = tk.Label(
            panel, text="", bg=self.config.COLOR_WHITE, fg="#777777",
            font=("Arial", 10))
        self.serial_hint_label.pack(pady=(0, 6))

        self.scan_status_label = tk.Label(panel, text="",
                                         bg=self.config.COLOR_WHITE,
                                         font=("Arial", 13, "bold"),
                                         wraplength=520, justify="center")
        self.scan_status_label.pack(pady=6)

        self.confirm_btn = tk.Button(
            panel, text="URUCHOM TEST", bg=self.config.COLOR_ACCENT,
            fg=self.config.COLOR_WHITE, font=("Arial", 15, "bold"),
            width=22, height=2, relief=tk.FLAT, cursor="hand2",
            command=self._process_serial)
        self.confirm_btn.pack(pady=(8, 28), padx=50)

        if self.integrity.blocked:
            self.serial_entry.config(state="disabled")
            self.confirm_btn.config(state="disabled")
            self.scan_status_label.config(
                text="Testowanie zablokowane — zgłoś błąd",
                fg=self.config.COLOR_ERROR)

    def _create_profile_selector(self, parent) -> None:
        """Lista rozwijana z profilami WLACZONYMI na tym stanowisku.

        Lista jest scisle powiazana z zakladka Profile w panelu inzynieryjnym:
        pokazuje dokladnie te profile, ktore sa tam zaznaczone. Po zamknieciu
        panelu ekran jest przebudowywany, wiec lista nie moze sie rozjechac
        z konfiguracja.

        W1: przy wiecej niz jednym wlaczonym profilu lista startuje PUSTA.
        Wczesniej domyslnym wyborem byl pierwszy profil alfabetycznie (czyli
        ER115), a ``show_scan_screen()`` odbudowuje ekran po KAZDEJ sztuce -
        operator pracujacy na SR213 cicho wracal na ER115 po kazdym tescie.
        Profile roznia sie kanalami, wiec czesc portow nie bylaby testowana
        wcale, a wynik wygladalby na normalny PASS.

        W2: zamiast pytania TAK/NIE (ktore przy jednym profilu nie pojawialo
        sie nigdy, a przy czytniku kodow bylo odpowiadane Enterem na "NIE")
        aktywny profil jest pokazany STALE, razem z napieciami i kanalami.
        Informacji na ekranie nie da sie odkliknac.
        """
        self._profile_choices: dict[str, Any] = {}
        labels: list[str] = []
        for profile in self.catalog.all():
            if not self.config.is_product_enabled(profile.product_id):
                continue
            label = profile.display_name
            if label in self._profile_choices:
                label = f"{profile.display_name} ({profile.product_id})"
            self._profile_choices[label] = profile
            labels.append(label)

        row = tk.Frame(parent, bg=self.config.COLOR_WHITE)
        row.pack(pady=(0, 6), padx=50, fill=tk.X)
        tk.Label(row, text="Profil testowy:", bg=self.config.COLOR_WHITE,
                 fg="#444444", font=("Arial", 13, "bold")).pack(side=tk.LEFT,
                                                                padx=(0, 10))

        # Lista rozwijana Tk ma WLASNA czcionke, niezaleznie od czcionki pola.
        # Bez ustawienia obu operator widzi duzy tekst w polu i drobny
        # w rozwinietej liscie - czyli dokladnie tam, gdzie wybiera profil.
        self._style_profile_combobox()

        # Jeden wlaczony profil - nie ma czego wybierac, ustawiamy od razu.
        # Wiecej niz jeden - pole startuje puste i operator MUSI wskazac.
        initial = labels[0] if len(labels) == 1 else ""
        self._selected_profile_var = tk.StringVar(value=initial)
        self.profile_combo = ttk.Combobox(
            row, textvariable=self._selected_profile_var, values=labels,
            state="readonly" if len(labels) > 1 else "disabled",
            font=("Arial", 16, "bold"), width=24,
            style="Profil.TCombobox")
        self.profile_combo.pack(side=tk.LEFT, fill=tk.X, expand=True,
                                ipady=6)
        self.profile_combo.bind("<<ComboboxSelected>>",
                                lambda event: self._on_profile_selected())

        # STALY pasek z aktywnym profilem i jego napieciami - zastepuje
        # pytanie potwierdzajace. Jest widoczny przez caly czas pracy.
        self.profile_banner = tk.Frame(parent, bg=self.config.COLOR_ACTION_BG,
                                       relief=tk.SOLID, borderwidth=1)
        self.profile_banner.pack(fill=tk.X, padx=50, pady=(10, 6))
        self.profile_banner_title = tk.Label(
            self.profile_banner, text="", bg=self.config.COLOR_ACTION_BG,
            fg=self.config.COLOR_ACTION, font=("Arial", 18, "bold"))
        self.profile_banner_title.pack(pady=(8, 0))
        self.profile_info_label = tk.Label(
            self.profile_banner, text="", bg=self.config.COLOR_ACTION_BG,
            fg="#1B3A57", font=("Arial", 12), justify="center",
            wraplength=560)
        self.profile_info_label.pack(pady=(2, 9), padx=12)
        self._on_profile_selected()

    def _style_profile_combobox(self) -> None:
        """Powieksza czcionke pola ORAZ rozwijanej listy.

        Lista rozwijana comboboxa to widget Tk (Listbox) tworzony wewnetrznie
        przez ttk, nieosiagalny normalnym API - jedyna droga to opcja
        w bazie opcji Tk. Bez tego ustawienie ``font`` w Combobox powieksza
        wylacznie zamkniete pole.
        """
        try:
            style = ttk.Style()
            style.configure("Profil.TCombobox", arrowsize=22, padding=6)
            self.root.option_add("*TCombobox*Listbox.font",
                                 ("Arial", 16, "bold"))
            self.root.option_add("*TCombobox*Listbox.selectBackground",
                                 self.config.COLOR_PRIMARY)
            self.root.option_add("*TCombobox*Listbox.selectForeground",
                                 self.config.COLOR_WHITE)
        except tk.TclError as exc:
            # Styl to kosmetyka - jej brak nie moze zablokowac ekranu.
            print(f"[GUI] Nie udało sie ustawić stylu listy profili: {exc}")

    def _force_uppercase_serial(self, *_args) -> None:
        """Podnosi zawartosc pola S/N do wielkich liter.

        Praca idzie przez ``after_idle`` i przez SAM WIDGET, nie przez
        zmienna: ustawienie StringVar wewnatrz jego wlasnego trace'a
        aktualizuje zmienna, ale Tk nadpisuje potem tresc pola stara
        wartoscia - operator widzialby male litery mimo poprawnej zmiennej.
        """
        widget = getattr(self, "serial_entry", None)
        if not self._widget_alive(widget) or self._uppercase_pending:
            return
        self._uppercase_pending = True
        try:
            self.root.after_idle(self._apply_uppercase_serial)
        except tk.TclError:
            self._uppercase_pending = False

    def _apply_uppercase_serial(self) -> None:
        self._uppercase_pending = False
        widget = getattr(self, "serial_entry", None)
        if not self._widget_alive(widget):
            return
        try:
            current = widget.get()
            upper = current.upper()
            if upper == current:
                return
            cursor = widget.index(tk.INSERT)
            state = widget.cget("state")
            if state != "normal":
                widget.config(state="normal")
            widget.delete(0, tk.END)
            widget.insert(0, upper)
            widget.icursor(cursor)
            if state != "normal":
                widget.config(state=state)
        except tk.TclError:
            # Okno moglo zostac zamkniete zanim doszlo do bezczynnosci.
            return

    def _selected_profile(self):
        return self._profile_choices.get(self._selected_profile_var.get())

    def _on_profile_selected(self) -> None:
        """Odswieza staly pasek aktywnego profilu.

        Pasek podaje NAPIECIA i kanaly, czyli faktyczne konsekwencje wyboru.
        Poprzednia wersja pytala "Czy na pewno chcesz wybrać profil X?" -
        pytanie bez zadnej informacji, na podstawie ktorej da sie odpowiedziec,
        wiec przy setnym powtorzeniu bylo odklikiwane odruchowo.
        """
        profile = self._selected_profile()

        if profile is None:
            if not self._profile_choices:
                self.profile_banner.config(bg=self.config.COLOR_ERROR)
                self.profile_banner_title.config(
                    text="BRAK PROFILU", bg=self.config.COLOR_ERROR,
                    fg=self.config.COLOR_WHITE)
                self.profile_info_label.config(
                    text="Żaden profil nie jest włączony na tym stanowisku.",
                    bg=self.config.COLOR_ERROR, fg="#FFEBEE")
            else:
                self.profile_banner.config(bg=self.config.COLOR_ACTION_BG)
                self.profile_banner_title.config(
                    text="WYBIERZ PROFIL", bg=self.config.COLOR_ACTION_BG,
                    fg=self.config.COLOR_ACTION)
                self.profile_info_label.config(
                    text="Rozwiń listę powyżej i wskaż wyrób, który testujesz.",
                    bg=self.config.COLOR_ACTION_BG, fg="#1B3A57")
            self._set_serial_hint("")
            return

        if not profile.matches_instrument(self.config.INSTRUMENT_MODEL):
            self.profile_banner.config(bg=self.config.COLOR_ERROR)
            self.profile_banner_title.config(
                text=profile.display_name, bg=self.config.COLOR_ERROR,
                fg=self.config.COLOR_WHITE)
            self.profile_info_label.config(
                text=f"Ten profil wymaga Chromy "
                     f"{'/'.join(profile.allowed_models)}, a stanowisko ma "
                     f"{self.config.INSTRUMENT_MODEL}.\nZgłoś błąd.",
                bg=self.config.COLOR_ERROR, fg="#FFEBEE")
            self._set_serial_hint("")
            return

        self.profile_banner.config(bg=self.config.COLOR_ACTION_BG)
        self.profile_banner_title.config(
            text=profile.display_name, bg=self.config.COLOR_ACTION_BG,
            fg=self.config.COLOR_ACTION)
        self.profile_info_label.config(
            text=self._profile_summary(profile),
            bg=self.config.COLOR_ACTION_BG, fg="#1B3A57")
        self._set_serial_hint(
            "Numer seryjny: "
            + " lub ".join(str(x) for x in profile.serial_lengths)
            + " znaków (A-Z, 0-9)")

    @staticmethod
    def _profile_summary(profile) -> str:
        """Napiecia i kanaly - najwazniejsza konsekwencja wyboru profilu."""
        voltages: list[str] = []
        for step in profile.steps:
            text = f"{float(step['voltage']) / 1000:.2f} kV"
            if text not in voltages:
                voltages.append(text)
        channels = []
        for step in profile.steps:
            mask = str(step.get("channels", ""))
            channels.extend(str(i) for i, s in enumerate(mask, 1) if s == "H")
        line = (f"{profile.step_count} kroków · "
                f"{' i '.join(voltages)} · "
                f"{profile.total_duration:.0f} s")
        if channels:
            line += f"\nkanały {', '.join(channels)}"
        return line

    def _set_serial_hint(self, text: str) -> None:
        label = getattr(self, "serial_hint_label", None)
        if not self._widget_alive(label):
            # Podpowiedz jest budowana PO liscie profili, wiec przy pierwszym
            # przejsciu jeszcze nie istnieje. Po przebudowie ekranu atrybut
            # moze tez wskazywac zniszczony widget - oba przypadki sa
            # normalne i nie moga przewrocic aplikacji.
            return
        try:
            label.config(text=text)
        except tk.TclError as exc:
            print(f"[GUI] Nie ustawiono podpowiedzi S/N: {exc}")

    def _process_serial(self) -> None:
        if self._scan_pending:
            return

        if self.integrity.blocked:
            self._reject_scan(
                "Testowanie zablokowane — profile nie zgadzają się "
                "z manifestem.")
            return

        # W1: profil MUSI byc wskazany swiadomie. Wczesniej pole startowalo
        # z pierwszym profilem alfabetycznie i resetowalo sie po kazdej sztuce.
        selected = self._selected_profile()
        if selected is None:
            if not self._profile_choices:
                self._reject_scan(
                    "Żaden profil nie jest włączony na tym stanowisku")
            else:
                self._reject_scan("Najpierw wybierz profil testowy")
                try:
                    self.profile_combo.focus_set()
                except tk.TclError:
                    pass
            return

        raw = self.serial_entry.get().strip().upper()
        if not raw:
            self.scan_status_label.config(text="Zeskanuj numer seryjny",
                                          fg=self.config.COLOR_WARNING)
            self.serial_entry.focus_set()
            return

        from product_profile import resolve_serial

        valid, outcome = resolve_serial(selected, raw)
        if not valid:
            self._reject_scan(str(outcome))
            return
        scan = outcome

        if not self.config.is_product_enabled(scan.product_id):
            self._reject_scan(
                f"Profil {scan.profile.display_name} jest wyłączony na tym "
                f"stanowisku — włącz go w panelu inżynieryjnym")
            return

        if not scan.profile.matches_instrument(self.config.INSTRUMENT_MODEL):
            self._reject_scan(
                f"{scan.profile.display_name} wymaga Chromy "
                f"{'/'.join(scan.profile.allowed_models)}, a to stanowisko ma "
                f"{self.config.INSTRUMENT_MODEL}")
            return

        self.scan_status_label.config(
            text=f"✓ {scan.serial} — {scan.profile.display_name}",
            fg=self.config.COLOR_ACCENT)

        self._scan_pending = True
        self.serial_entry.config(state="disabled")
        self.confirm_btn.config(state="disabled")
        self._scan_after_id = self.root.after(
            300, lambda: self._show_test_screen(scan))

    def _reject_scan(self, message: str) -> None:
        self.scan_status_label.config(text=f"✗ {message}",
                                      fg=self.config.COLOR_ERROR)
        try:
            self.serial_entry.delete(0, tk.END)
            self.serial_entry.focus_set()
        except tk.TclError:
            pass

    def _show_test_screen(self, scan) -> None:
        self._scan_after_id = None
        screen = None
        try:
            from test_screen import TestScreen

            screen = TestScreen(parent=self.root, config=self.config, scan=scan,
                                app_ref=self)
            self.current_test_screen = screen
            screen.show()
        except Exception as exc:
            print(f"[GUI] {exc!r}")
            traceback.print_exc()
            if screen is not None:
                try:
                    screen.shutdown()
                except Exception:
                    pass
            self.current_test_screen = None
            self._scan_pending = False
            messagebox.showerror(
                "Błąd otwierania ekranu testowego",
                f"Ekran testowy nie został uruchomiony:\n{exc}",
                parent=self.root)
            self.show_scan_screen()

    # ------------------------------------------------------------------ #
    # PANEL ADMINISTRATORA
    # ------------------------------------------------------------------ #
    def _bind_admin_shortcut(self) -> None:
        self._shortcut_presses = 0
        self._shortcut_timer = None
        self.root.bind("<Control-Alt-d>", self._on_config_shortcut)
        self.root.bind("<Control-Alt-D>", self._on_config_shortcut)

    def _on_config_shortcut(self, event=None) -> None:
        if self.current_test_screen is not None or self._scan_pending:
            return
        self._shortcut_presses += 1
        if self._shortcut_timer:
            self.root.after_cancel(self._shortcut_timer)
        self._shortcut_timer = self.root.after(
            1000, lambda: setattr(self, "_shortcut_presses", 0))
        if self._shortcut_presses >= 3:
            self._shortcut_presses = 0
            self._show_password_dialog()

    def _show_password_dialog(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Panel inżynieryjny")
        window.geometry("420x240")
        window.configure(bg=self.config.COLOR_BG)
        window.resizable(False, False)
        window.transient(self.root)
        window.grab_set()

        frame = tk.Frame(window, bg=self.config.COLOR_WHITE, relief=tk.RAISED,
                         borderwidth=2)
        frame.pack(expand=True, fill=tk.BOTH, padx=20, pady=20)
        tk.Label(frame, text="Panel inżynieryjny",
                 bg=self.config.COLOR_WHITE, fg=self.config.COLOR_PRIMARY,
                 font=("Arial", 14, "bold")).pack(pady=(18, 8))
        tk.Label(frame, text="Wprowadź hasło:", bg=self.config.COLOR_WHITE,
                 fg="#333333", font=("Arial", 11)).pack(pady=(6, 4))

        entry = tk.Entry(frame, font=("Arial", 12), width=22, justify="center",
                         show="*", relief=tk.SOLID, borderwidth=2)
        entry.pack(pady=8)
        entry.focus()

        error_label = tk.Label(frame, text="", bg=self.config.COLOR_WHITE,
                               fg=self.config.COLOR_ERROR, font=("Arial", 9))
        error_label.pack()

        if self._admin_gate.is_locked():
            error_label.config(
                text=f"Dostęp zablokowany na "
                     f"{self._admin_gate.seconds_remaining():.0f} s")
            entry.config(state="disabled")

        def check_password() -> None:
            if self._admin_gate.is_locked():
                error_label.config(
                    text=f"Dostęp zablokowany na "
                         f"{self._admin_gate.seconds_remaining():.0f} s")
                entry.delete(0, tk.END)
                return
            try:
                ok = verify_password(
                    entry.get(), getattr(self.config, "ADMIN_PASSWORD", None))
            except Exception as exc:
                print(f"[GUI] {exc!r}")
                traceback.print_exc()
                audit("PANEL/BLAD_REKORDU_HASLA", str(exc))
                error_label.config(text=f"Konfiguracja hasła uszkodzona: {exc}")
                entry.delete(0, tk.END)
                return

            if ok:
                self._admin_gate.reset()
                audit("PANEL/WEJSCIE", "")
                window.destroy()
                self._show_admin_panel()
                return

            lockout = self._admin_gate.register_failure()
            if lockout:
                audit("PANEL/BLOKADA", f"{lockout:.0f} s po nieudanych probach")
                error_label.config(text=f"Zbyt wiele prób — blokada "
                                        f"{lockout:.0f} s")
                entry.config(state="disabled")
                window.after(int(lockout * 1000), lambda: (
                    entry.config(state="normal"), error_label.config(text=""),
                    entry.focus()) if window.winfo_exists() else None)
            else:
                audit("PANEL/BLEDNE_HASLO",
                      f"pozostalo prob: {self._admin_gate.attempts_left()}")
                error_label.config(
                    text=f"Nieprawidłowe hasło! (pozostało prób: "
                         f"{self._admin_gate.attempts_left()})")
            entry.delete(0, tk.END)
            entry.focus()

        entry.bind("<Return>", lambda event: check_password())
        buttons = tk.Frame(frame, bg=self.config.COLOR_WHITE)
        buttons.pack(pady=10)
        tk.Button(buttons, text="OK", bg=self.config.COLOR_ACCENT,
                  fg=self.config.COLOR_WHITE, font=("Arial", 10, "bold"),
                  width=10, relief=tk.FLAT, cursor="hand2",
                  command=check_password).pack(side=tk.LEFT, padx=5)
        tk.Button(buttons, text="Anuluj", bg="#999999",
                  fg=self.config.COLOR_WHITE, font=("Arial", 10, "bold"),
                  width=10, relief=tk.FLAT, cursor="hand2",
                  command=window.destroy).pack(side=tk.LEFT, padx=5)

    def _show_admin_panel(self) -> None:
        if self.current_test_screen is not None or self._scan_pending:
            messagebox.showwarning(
                "Panel zablokowany",
                "Ustawień nie można zmieniać na ekranie testowym. "
                "Wróć do menu głównego.",
                parent=self.root)
            return
        panel = AdminPanel(self.root, self.config, self.catalog)
        panel.show()
        # Zestaw wlaczonych profili moze sie zmienic w panelu - po jego
        # zamknieciu przebudowujemy ekran, zeby lista rozwijana zawsze
        # odpowiadala zakladce Profile.
        if panel.window is not None:
            self.root.wait_window(panel.window)
        self.show_scan_screen()
