# Zmiany — 17.09.2026

## Powrót do skanowania po przerwanym teście

Po przerwaniu testu (otwarta klapa albo STOP) jedynym wyjściem był „Powrót
do menu" — czyli rozłączenie testera, przebudowa ekranu i pełna procedura
połączenia od nowa. Przy przypadkowym otwarciu klapy nieproporcjonalne.

Przycisk **„➜ Następny SN" zmienia rolę**: po przerwaniu staje się zielonym
**„➜ Przygotuj kolejną sztukę"**. Aplikacja przeprogramowuje kroki w testerze
i sama otwiera okno skanowania — bez rozłączania i bez powrotu do menu.
Programowanie idzie w tle, więc okno nie zamarza.

Świeże przejście OPEN → CLOSED jest nadal wymagane przed startem —
przerwanie niczego tu nie skraca.

Pokryte testem `test_recovery_after_abort` (sprawdzone, że bez poprawki
test upada).

## Numer seryjny w logu sesji

Linie `[WYNIK]` nie pokazywały, do której sztuki należy cykl. Przy dwóch
cyklach zapisanych do tego samego pliku nie dało się z logu rozstrzygnąć,
czy to ten sam numer, czy błąd przypisania. Dodane:

    [TEST] Start cyklu | S/N <numer> | profil <id>
    [TEST] Wynik PASS  | S/N <numer> | profil <id>
    [LOG]  Zapisano raport S/N <numer>: <ścieżka>

## Kontrola sum profili — komunikat przeniesiony

Informacja o rodzaju manifestu (lokalny kontra zewnętrzny) zniknęła z ekranu
operatora i trafiła do panelu inżynieryjnego, zakładka **Profile**. Operator
nie miał jak na nią zareagować, a stałe ostrzeżenie bez możliwości działania
uczy ignorowania ostrzeżeń — również tego czerwonego, który naprawdę blokuje
testowanie.

Na ekranie startowym zostaje wyłącznie czerwony pas blokujący.

## Przerwany przebieg — gdzie szukać

Przerwany cykl **celowo nie tworzy pliku raportu**: raporty są zaciągane
przez webservice jako wyniki testu, więc wpis dla przerwanego przebiegu
oznaczyłby dobrą sztukę jako przetestowaną. Ślad jest w dwóch miejscach:

    app_runtime_logs\config_audit.log   ->  TEST/PRZERWANY S/N ...
    app_runtime_logs\hipot_RRRRMMDD.log ->  [PRZEBIEG] PRZERWANY | S/N ...

oraz w panelu „Ostatnie wyniki" na ekranie testowym.
