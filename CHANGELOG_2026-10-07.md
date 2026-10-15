# Zmiany — 07.10.2026

Podstawa: `hipot_20261007.log` i `config_audit.log` ze stanowiska, build 1.0.3.

## Co już działa

Build 1.0.3 (paczka z 28.09 + limity z 02.10) jest na stanowisku i robi
dokładnie to, co miał:

```
[WYNIK] Kod nieterminalny na kroku 2 (JUDG 112). Kody wszystkich krokow:
        krok 1 (Ethernet 1)=18; krok 2=112; krok 3=112; krok 4=112; krok 5=112
[WYNIK] Sekwencja zatrzymana na kroku 1 (Ethernet 1, kod 18)
        - kroki 2-5 nie zostaly wykonane
[WYNIK] Krok 1 (Ethernet 1): FAIL kod=18 1061 V 0.0270 mA
[TEST]  Wynik FAIL | S/N D201197B032496
[LOG]   Zapisano raport S/N D201197B032496: Z:\D201197B032496.txt
```

**Pierwsze FAIL-e w historii tego stanowiska — cztery tego dnia**, z raportem
na `Z:\`. Kod 18 to `AC Low Fail`, prąd 0,027 mA przy progu 0,035 mA. Bez
poprawki limitu z 02.10 ta sztuka w ogóle by się nie zmierzyła, bo próg
wynosił 0,200 mA.

## Poprawione: zwarcie pokazywane jako awaria stanowiska

Sztuka **D210243B035775** dziesięć razy z rzędu skończyła jako
`TEST/AWARIA — Chroma nie potwierdziła rozpoczęcia NOWEGO cyklu`.
Po wymianie kondensatora na porcie Ethernet 1 przeszła normalnie — czyli
wyrób był wadliwy, a aplikacja wskazała stanowisko.

**Mechanizm.** Zwarcie wywraca tester w kilkanaście milisekund. Jeden obrót
odpytania przy 9600 bodach trwa ~185 ms. Pętla potwierdzania startu szuka
stanu `TESTING` albo napięcia powyżej 50 V — przy takiej sztuce nie pada ani
jedno, bo zanim doszło pierwsze zapytanie, tester był już po werdykcie.
Po 3 s pętla uznawała, że cykl nie ruszył.

W logu widać to co do dziesiątej części sekundy:

```
09:09:43.99  [START] ERR='+0,"No error"', KLOC='1'
09:09:47.13  [START] Brak potwierdzenia nowego aktywnego cyklu
```

3,14 s — czyli dokładnie wyczerpany limit, ani jednego odczytu stanu
aktywnego.

**Rozwiązanie.** Aplikacja czyta status **tuż przed** wysłaniem `SAFEty:STARt`
(po naszym własnym STOP tester stoi na `STOPPED`) i zapamiętuje go. Jeśli po
STARcie status przejdzie na `PASS` albo `FAIL`, którego przed startem nie
było, cykl jest uznany za wykonany — bo werdykt może powstać wyłącznie
z cyklu testowego.

Rozstrzyga **przejście** statusu, nie sam jego odczyt. Gdyby wystarczał sam
odczyt `FAIL`, aplikacja przypisałaby werdykt poprzedniej sztuki nowemu
numerowi seryjnemu. Jest na to osobny przypadek w teście.

Odczyt statusu idzie przed zapisem `STARt`, więc nie zabiera czasu oknu
pomiarowemu kroku 1.

**Czego to nie osłabia:** natychmiastowy **PASS** nadal jest odrzucany —
przez warstwę dowodową (brak próbek obciążenia) i przez bramkę
`PASS_MIN_RUNTIME_FRACTION`. Ta zmiana może przepuścić wyłącznie FAIL.

## Testy

39 testów regresyjnych, nowy `test_instant_trip_is_a_fail_not_a_station_failure`.
Sprawdzone w obie strony: bez poprawki test upada, a przy poprawce zbyt
pobłażliwej (przyjmującej sam odczyt `FAIL` bez porównania) też upada.

## Uwagi do stanowiska

W dzienniku audytowym z 05–07.10 widać kilkanaście zmian portów i baudrate
(`COM2 ↔ COM4 ↔ COM6`, `9600 ↔ 19200`). Jeśli to szukanie właściwego portu
po przepięciu kabli — warto to raz ustalić i zapisać, bo każda zmiana
wymaga ponownego połączenia i programowania kroków.
