# Reconext Hi-Pot Main Units v1.0.0 — silnik uniwersalny

Jedna aplikacja dla wszystkich wyrobów testowanych na Chromie. Różnica między
produktami to **plik JSON**, nie kod. W paczce jest jeden profil —
`SR203_SR204` — na Chromę 19053 ze scan boxem. Silnik obsługuje też Chromę
19052 i profile bez scan boxa, tylko nie ma dziś takiego wyrobu w katalogu.

Identyfikatory profili zapisujemy **WIELKIMI literami** (`SR203_SR204`), tak jak
nazwy wyrobów. Porównania są niewrażliwe na wielkość liter — ręcznie wpisane
małe litery zostaną znormalizowane przy wczytaniu.

---

## 1. Co gdzie leży

```
Reconext Hi-Pot Main Units/
├─ Reconext Hi-Pot Main Units.exe
├─ station_config.json     ← USTAWIENIA STANOWISKA (porty, model, logi, hasło,
│                             aktywne profile)
├─ hwid_map.json           ← HWID → produkt + nazwa modelu
├─ products/
│   └─ SR203_SR204.json    ← PROFIL PRODUKTU (napięcia, limity, kroki, kanały)
├─ approved_sources.json   ← zatwierdzone sumy SHA-256 (kontrola wydania)
├─ app_runtime_logs/       ← dziennik sesji + config_audit.log
└─ logs_pending/           ← raporty awaryjne, gdy \\IFS niedostępne
```

Rozdział jest celowy: **przeniesienie stanowiska na inny COM nie dotyka profilu
testowego, a zmiana profilu nie dotyka ustawień stanowiska.** Profile są
identyczne na wszystkich stanowiskach i podlegają zatwierdzeniu; konfiguracja
stanowiska jest lokalna.

---

## 2. Przepływ operatora

Bez logowania — operator skanuje SN i pracuje.

1. Wybiera profil z listy (albo lista ma jeden pozycję i jest zablokowana),
   skanuje S/N. Skąd bierze się profil, decyduje pole `serial.identify_by`
   — patrz **2b**. Wyrób przypisany do profilu **wyłączonego na tym
   stanowisku** jest odrzucany z czytelnym komunikatem.
2. Aplikacja łączy się z Chromą, sprawdza `*IDN?` **i model** względem
   `INSTRUMENT_MODEL`, kasuje stare kroki, programuje wszystkie kroki profilu,
   ustawia maski kanałów i **odczytuje wszystko zwrotnie**.
3. Otwarcie i zamknięcie klapy (OPEN → CLOSED) startuje cykl.
4. Chroma wykonuje 5 kroków w jednym cyklu; aplikacja przypisuje próbki do
   kroków po polu STEP z `SAFEty:FETCh?`.
5. Wynik: PASS tylko gdy **wszystkie** kroki PASS **i** każdy krok ma dowody
   z pomiaru na żywo. Raport TXT z rozbiciem STEP 1..5.
6. Okno „następny SN" — kolejna sztuka bez powrotu do menu.

Interfejs jest celowo pusty: nagłówek to nazwa aplikacji, stopka to
`Reconext Hi-Pot Main Units v1.0.0` po lewej i `Autor: Kacper Urbanowicz`
po prawej. Nic więcej.

---

## 2a. Profile aktywne na stanowisku

Ta sama aplikacja stoi w różnych miejscach, a w każdym testuje się inne wyroby.
Panel inżynieryjny → zakładka **Profile** → checkboxy **Aktywne na tym
stanowisku**.

- Zaznaczenie wszystkich zapisuje `ENABLED_PRODUCTS` jako **brak ograniczenia**,
  więc dodanie nowego profilu nie wymaga potem obchodzenia każdego stanowiska.
- Odznaczenie wszystkich jest blokowane — stanowisko bez żadnego profilu nie
  przetestowałoby niczego.
- Lista trafia do `station_config.json`, **nie** do plików profili. Profile mają
  zostać identyczne wszędzie; różny jest tylko zestaw wyrobów w danym miejscu.
- Każda zmiana idzie do dziennika audytowego.

---

## 2b. Skąd aplikacja wie, który profil uruchomić

Pole `serial.identify_by` w profilu:

| Wartość | Źródło profilu | Numer seryjny |
|---|---|---|
| `hwid` (domyślne) | mapa HWID — pierwsze 6 znaków S/N | długość z profilu + prefiks musi być w mapie; wybór z listy **musi** się zgadzać z HWID |
| `operator` | lista rozwijana na ekranie startowym | tylko długość i zestaw znaków |

`SR203_SR204` używa `operator`: numery seryjne tych wyrobów nie niosą
informacji o modelu, więc mapa HWID nie ma czego rozstrzygać i **może zostać
pusta**. S/N musi mieć **dokładnie 14 znaków**, wyłącznie wielkie litery A–Z
i cyfry 0–9. Małe litery są podnoszone już w trakcie wpisywania, żeby operator
widział na ekranie dokładnie to, co trafi do raportu i do nazwy pliku.

Jeżeli mimo `operator` prefiks sztuki **jest** opisany w mapie HWID i wskazuje
inny profil — skan zostaje odrzucony. Kosztuje to nic, a chroni stanowisko
mieszane.

Profil z `identify_by: "hwid"`, który nie ma ani jednego wpisu w mapie,
zatrzymuje **build** (preflight `create_exe.py`) — inaczej stanowisko
odrzucałoby każdy skan.

---

## 3. Profil produktu — pola

```json
{
  "product_id": "SR203_SR204",
  "display_name": "SR203 / SR204",
  "instrument": {
    "allowed_models": ["19053"],
    "requires_scan_box": true,
    "channel_count": 8
  },
  "serial": { "allowed_lengths": [14], "identify_by": "operator" },
  "test_timeout_s": 60,
  "steps": [
    {
      "name": "Ethernet 1",
      "mode": "ACW",
      "voltage": 1060,
      "limit_high": 1.0,
      "limit_low": 0.2,
      "presence_min_current": 0.2,
      "ramp_time": 0.5, "dwell": 1.0, "ramp_dn": 0.5,
      "arc_sense": 0, "frequency": 60, "continuity": "OFF",
      "channels": "HOOOOOOO"
    }
  ]
}
```

Maska kanałów: `O` = Open, `H` = High (napięcie), `L` = Low (powrót). Zapis
`O,O,H,O,O,O,O,O` też jest akceptowany. Wymagany co najmniej jeden `H` — maska
bez `H` nie podaje napięcia na produkt i zawsze zmierzyłaby prąd bliski zeru.

### Nazewnictwo pól — 1:1 z oprogramowaniem Chromy

Panel inżynieryjny (tabela kroków i okno edycji) używa nazw z zakładki
**Parameters** w oprogramowaniu testera, żeby technolog porównywał profil
z ekranem Chromy bez tłumaczenia nazw:

| Nazwa w panelu | Klucz w JSON | Uwaga |
|---|---|---|
| Ext. Name | `name` | |
| Voltage | `voltage` | JSON w woltach, panel w kV |
| High Limit | `limit_high` | mA |
| Low Limit | `limit_low` | mA |
| ARC Limit | `arc_sense` | mA, 0 = wyłączony |
| Test Time | `dwell` | s |
| Ramp Time | `ramp_time` | s |
| Fall Time | `ramp_dn` | s |
| Real Current | `real_limit` | mA, 0 = wyłączony |
| Channel | `channels` | maska O/H/L |
| **Obecność** | `presence_min_current` | **pole aplikacji, nie ma go w testerze** — patrz niżej |

Klucze JSON zostały bez zmian, żeby istniejące profile i mapy nadal się
wczytywały; zmieniły się wyłącznie etykiety w interfejsie.

### Kolejność kroków

Kolejność w tablicy `steps` **jest kolejnością testu** — krok 1 w pliku to
`STEP 1` w Chromie i `STEP: 1` w raporcie. Zmienia się ją w panelu
inżynieryjnym: zakładka **Profile** → zaznacz krok → **▲ W górę** / **▼ W dół**
→ **Zapisz profil**. Przed zapisem panel pokazuje starą i nową kolejność do
potwierdzenia, a zmiana trafia do dziennika audytowego jako
`PROFIL/<produkt>/KOLEJNOSC`. Parametry i maska kanałów jadą razem z krokiem —
przestawienie nie podmienia napięcia ani kanału.

### Próg obecności — zmiana względem poprzedniej aplikacji

Aplikacja jednoproduktowa miała **stałą** `MIN_PRESENCE_CURRENT_MA = 0.500`
dobraną do 4 kV. Dla SR203/SR204 (1,06 kV, Max Limit 1,0 mA) taki próg jest bez
sensu — tam rolę detekcji obecności pełni Low Limit 0,05–0,2 mA. W silniku
próg jest polem **kroku**, ale nadal obwarowany trzema regułami, których nie da się obejść
z panelu ani z pliku:

1. `presence_min_current > 0`,
2. `>= ABSOLUTE_MIN_PRESENCE_MA` (0,010 mA — poniżej tego pomiar tonie w szumie
   własnym miernika),
3. `>= limit_low` i `< limit_high`.

Zmiana bezwzględnej granicy wymaga edycji `safety_rules.py` i przejścia
release — nie da się jej wyklikać.

---

## 4. Składnia SCPI — zweryfikowana z manualem

Podstawa: *HIPOT Tester 19051/19052/19053/19054 User's Manual*, wersja 2.1,
grudzień 2009, P/N A11 000893, rozdział 5. Wszystkie komendy w `scpi_dialect.py`
są zgodne z tym dokumentem.

### Kanały scan boxa: LISTY, nie komenda na kanał (s. 5-21/5-22)

```
SAFE:STEP1:AC:CHAN (@(1,3))       → kanały 1 i 3 jako HIGH
SAFE:STEP1:AC:CHAN (@(0))         → żaden kanał HIGH
SAFE:STEP1:AC:CHAN:LOW (@(2,4))   → kanały 2 i 4 jako LOW/RTN
SAFE:STEP1:AC:CHAN?               → "(@(1,3))"
```

Maska profilu `OOHOOOOO` jest tłumaczona na `HIGH=(@(3))`, `LOW=(@(0))`.
Dla SR203/SR204 strona LOW jest pusta — powrót idzie przez stały zacisk
RTN/LOW na płycie czołowej, dokładnie jak w profilu `SR203,204.stp`.

### Parametry, o których poprzednia wersja nie wiedziała

| Parametr | Ścieżka | Uwaga |
|---|---|---|
| Arc Sense | `SAFE:STEP<n>:AC:LIM:ARC <A>` | s. 5-20 — poprzednia aplikacja używała `AC:ARC` i dostawała −113 |
| Real Current | `SAFE:STEP<n>:AC:LIM:REAL <A>` | s. 5-20 — kolumna „Real Current" w oprogramowaniu Chromy |
| Częstotliwość | `SAFE:PRES:AC:FREQ <50\|60>` | s. 5-32 — **globalna**, nie per krok |

Silnik programuje wszystkie trzy i potwierdza odczytem zwrotnym. Ponieważ
częstotliwość jest globalna, profil o różnych `frequency` w krokach jest
odrzucany przy konfiguracji.

### Odczyt zwrotny: jedno zapytanie na krok (s. 5-18)

```
SAFE:STEP1:SET?
→ 1, AC, 5.000000E+03, 6.000000E-04, 7.000000E-06, 8.000000E-03,
  3.000000E+00, 1.000000E+00, 2.000000E+00, 4.000000E-04, (@(0)), (@(0))
```
= STEP, MODE, VOLT, HIGH, LOW, ARC, TIME, RAMP, FALL, REAL, SCAN HI, SCAN LOW

Zastępuje osiem osobnych zapytań. Przy pięciu krokach i suficie 19200 bodów
to kilkadziesiąt round-tripów mniej na każdą konfigurację.

### Co nadal jest sprawdzane na sprzęcie

Manual opisuje firmware z 2009 r. Sonda **zostaje**: zakładka
**Diagnostyka SCPI** wysyła każdą komendę i sprawdza `SYST:ERR?` (tylko
zapytania — nie uruchamia testu ani nie podaje wysokiego napięcia). Odrzucenie
komendy wymaganej blokuje test z nazwą nagłówka do poprawienia w
`SCPI_OVERRIDES`. Maska kanałów jest zawsze potwierdzana odczytem — rozbieżność
przerywa konfigurację.

Po zaprogramowaniu aplikacja czyta `SAFE:SNUM?` i **wymaga**, by liczba kroków
równała się liczbie kroków profilu.

---

## 5. Baudrate — 19200, bo to maksimum testera

Manual rozdz. 6.2: `BAUD RATE: 300 / 600 / 1200 / 2400 / 4800 / 9600 / 19200`,
`FLOW CTRL.: NONE / SOFTWARE`. Sprzętowego RTS/CTS ten tester **nie ma** —
walidacja go odrzuca, żeby nie skończyło się martwym łączem.

Przy 9600 jedna iteracja odpytywania (status + `FETCh`) to ok. 100–150 ms.
Profil SR203/SR204 ma dwell **1,0 s na krok**, a dowody PASS wymagają minimum
2 próbek przy pełnym napięciu — margines jest cienki. Dlatego 19200 plus
skrócona przerwa w pętli (20 ms zamiast 100 ms).

Aplikacja mierzy rzeczywisty czas odpytywania i **ostrzega na ekranie**, jeśli
przy zadanym dwell nie zdąży zebrać wymaganych próbek.

---

## 6. Dostęp do panelu inżynieryjnego i ślad audytowy

**Hasło: `reconext2026`** — wejście przez trzykrotne `Ctrl+Alt+D`.

W kodzie źródłowym hasła **nie ma w postaci jawnej**; `station_config.json`
zawiera tylko skrót PBKDF2-HMAC-SHA256 (200 000 iteracji, losowa sól), więc nie
da się go wyciągnąć z EXE przez `strings`. Builder pilnuje, żeby literał nie
wrócił do kodu.

Co to daje, a czego nie: chroni przed odczytaniem hasła z binarki, **nie** przed
podmianą skrótu przez osobę z prawem zapisu do folderu aplikacji. Uprawnienia
NTFS na folderze stanowiska pozostają warunkiem koniecznym.

- 3 nieudane próby → blokada 30 s. Licznik żyje przez całą sesję aplikacji.
- Brak rekordu hasła w konfiguracji **zamyka** panel (nie otwiera go awaryjnie).
- Hasło można zmienić w zakładce Bezpieczeństwo, jeśli kiedyś będzie taka
  potrzeba.
- `app_runtime_logs/config_audit.log`: wejścia do panelu, nieudane próby,
  każda zmiana parametru w formie `stara → nowa`.
- Log sesji ma znaczniki czasu i kanał (`[OUT]`/`[ERR]`).

**Ograniczenie, które trzeba znać:** skrót w pliku chroni przed odczytaniem
hasła, nie przed jego podmianą przez osobę z prawem zapisu do folderu
aplikacji. Uprawnienia NTFS na folderze stanowiska (zapis tylko dla technologa)
to warunek konieczny, nie opcja.

---

## 7. Build i zwolnienie wersji

```
python create_exe.py --approve    # zatwierdź obecne sumy SHA-256 źródeł
python create_exe.py              # build (Windows, Python 3.13, PyInstaller 6.21.0)
```

Builder blokuje build, gdy:

- brakuje markera bezpieczeństwa w pliku źródłowym (np. ktoś wyciął
  `validate_step_pass_evidence`),
- pojawi się marker zabroniony (jawne hasło),
- suma SHA-256 nie zgadza się z `approved_sources.json`,
- wróciło logowanie operatora albo jawne hasło w kodzie,
- `station_config.json` nie zawiera rekordu hasła albo hasło standardowe
  przestało działać,
- testy regresyjne nie przechodzą,
- HWID wskazuje nieistniejący profil albo profil nie przechodzi walidacji.

---

## 8. Testy regresyjne

`python release_selftest.py` — 20 testów, bez podłączonej Chromy i Arduino.
Pokrywają m.in.:

| Test | Co pilnuje |
|---|---|
| `test_step_and_channel_rules` | maski kanałów, próg obecności, zakresy |
| `test_multistep_configuration_and_channel_readback` | maska niepotwierdzona odczytem blokuje test |
| `test_multistep_result_gate` | pusty port → PASS odrzucony mimo kodu 116 z testera |
| `test_fresh_cycle_guard` | stary wynik nie może zostać uznany za nowy |
| `test_device_identity_matches_station_model` | 19052 podłączony do stanowiska 19053 → odrzucony |
| `test_interlock_regressions` | błędy K1 i K3 z audytu 1.0.5 |
| `test_rs232_response_reassembly` | błąd K2 z audytu 1.0.5 |
| `test_dialect_and_channel_lists` | składnia list kanałów i kody wyniku wg manuala |
| `test_transport_limits` | 38400/57600/115200 i RTS/CTS odrzucone |
| `test_enabled_products_gate` | wyłączony profil nie da się uruchomić |
| `test_ui_has_no_operator_login` | logowanie operatora nie wróciło, stopka zgodna |
| `test_product_mismatch_blocks_next_serial` | inny produkt w oknie „następny SN" |

---

## 9. Dodanie kolejnego produktu

1. `products/<nazwa>.json` — profil ze wszystkimi krokami.
2. Panel → Mapa HWID → dodaj HWID z przypisaniem do profilu i nazwą modelu.
3. Panel → Diagnostyka SCPI → sonda (jeśli to nowy model testera).
4. `python create_exe.py --approve` i build.

Bez zmiany kodu.
