# Zmiany — 16.10.2026

## Panel inżynieryjny dopasowuje się do ekranu

Zgłoszenie: okno panelu jest za duże i nie widać zawartości.

Były dwa osobne problemy i oba są poprawione.

### 1. Okno wychodziło poza ekran

Rozmiar okna liczył się ze skali **pełnego ekranu**, a pełny ekran to nie
to samo co miejsce, które system faktycznie oddaje aplikacji. Na pasek
zadań schodziło 40–48 px u dołu, a przy pasku ustawionym z boku — 60–90 px
z boku. Okno jest modalne (`grab_set`), więc cokolwiek zjechało pod pasek
zadań, było dla technologa nieosiągalne — razem z przyciskiem
„Zapisz profil”.

Teraz na Windows aplikacja pyta system wprost o **obszar roboczy pulpitu**
(`SPI_GETWORKAREA`). Tylko system wie, gdzie stoi pasek zadań, jak jest
wysoki i czy nie stoi pionowo. Okno jest wyśrodkowane w tym obszarze i
nigdy nie jest od niego większe — rozmiar preferowany zawsze ustępuje
ekranowi.

Poza Windows (i gdyby odczyt się nie udał) zostaje oszacowanie
`ekran − 60 × 90 px`, czyli tak jak dotąd.

### 2. Zawartość zakładki była wyższa niż okno

Samo zmieszczenie okna na ekranie nie wystarcza. Zakładka **Profile**
potrzebuje ponad 700 px wysokości (tytuł, kontrola sum kontrolnych, lista
profili, długość S/N, tabela kroków, przyciski, komentarz). Na monitorze
1366×768 przy skalowaniu Windows 125% obszar roboczy ma efektywnie ~614 px,
z czego na treść zostaje ~450 px. Reszta po prostu nie istniała.

Każda zakładka ma teraz **własne przewijanie pionowe**. Pasek pokazuje się
wyłącznie wtedy, gdy treść się nie mieści — na dużym monitorze panel
wygląda dokładnie jak wcześniej. Działa kółko myszy, z jednym wyjątkiem:
nad tabelą kroków i nad tabelą diagnostyki kółko przewija **tabelę**, a nie
zakładkę, żeby nie przewijało się dwa razy naraz.

Dodatkowo:

* okno można **zmniejszyć ręcznie** — dolna granica to 640 × 400 px
  (albo rozmiar ekranu, jeśli jest mniejszy);
* tabela kroków dostała **poziomy pasek przewijania**. Ma 12 kolumn
  (~1060 px) i na wąskim monitorze ostatnie dwie — `Channel` i
  `Obecność` — wychodziły poza okno. To akurat te, przez które powstały
  pomyłki w profilach, więc muszą być dostępne.

## Czego to nie zmienia

Żadnej logiki testu, żadnego profilu, żadnego limitu. Zmiana dotyczy
wyłącznie rozmieszczenia okna panelu inżynieryjnego — ekran operatora
jest nietknięty.

## Testy

**40 testów regresyjnych.** Nowy:
`test_admin_panel_tabs_scroll_when_screen_is_short` — sprawdza, że zakładka
jest przewijalna, że pasek pojawia się i **znika** razem z wysokością
treści, że kółko myszy nad `Treeview` nie rusza zakładki i że kółko nie jest
podpięte globalnie (`bind_all` zostaje w interpreterze po zamknięciu panelu
i przewijałoby ekran operatora).

`test_admin_panel_fits_on_station_screens` liczy teraz wyśrodkowanie
względem obszaru roboczego, a nie pełnego ekranu — przy pasku zadań z boku
środek pulpitu nie jest środkiem ekranu.

Sprawdzone wizualnie na ekranie 1280×720: okno 1073×560, przycisk
„Zapisz profil” osiągalny po przewinięciu.
