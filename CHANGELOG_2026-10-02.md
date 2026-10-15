# Zmiany — 02.10.2026

Podstawa: zrzuty ekranu oryginalnego programu **`SR203,204.stp`** z aplikacji
Chroma Hipot Tester, ze stanowiska obok, na którym te same sztuki przechodzą
na PASS.

## Znalezione: nasz Low Limit był niemal sześć razy za wysoki

| parametr | `SR203,204.stp` | nasz profil (do 01.10) |
|---|---:|---:|
| Ethernet 1–4 — Low Limit | **0,035 mA** | 0,200 mA |
| Modem — Low Limit | **0,02 mA** | 0,050 mA |
| Modem — High Limit | **1,5 mA** | 1,000 mA |

Zmierzone prądy na dobrych sztukach to 0,34–0,41 mA. Przy progu 0,200 mA
margines wynosił **1,9×** — sztuka ciągnąca 0,18 mA dostawała `AC Low Fail`.
Przy progu 0,035 mA margines to **10×**, czyli wartość typowa dla kontroli
„czy coś jest podłączone".

To wyjaśnia cały wrzesień: Low Fail → tester (ustawienie **AFTER FAIL = STOP**)
zatrzymuje sekwencję → kroki po nim mają w rejestrze 112 → aplikacja wyrzuca
cały wynik jako „test nieważny".

**Skąd się wzięła zła wartość:** 26.08 odczytałem limity z programu zastanego
w pamięci testera i przyjąłem je za wzorzec. Wzorcem jest program
zatwierdzony, nie to, co akurat siedzi w testerze.

## Poprawione

`products/SR203_SR204.json` — Low Limit 0,035 / 0,02 mA, Modem High Limit
1,5 mA. Próg obecności zrównany z Low Limit.

Test `test_read_program_matches_station_readout` porównuje teraz profil
z wartościami z `.stp`, a przypadek wykrywania różnicy używa dokładnie tej
pomyłki (0,200 zamiast 0,035) — gdyby to porównanie uruchomiono 26.08,
różnica byłaby widoczna od razu.

38 testów regresyjnych przechodzi.

## NIEROZSTRZYGNIĘTE — kanały scan boxa

To wymaga Twojej weryfikacji i **nie zostało zmienione**.

| profil | Modem / DSL | Ethernet 1–4 |
|---|---|---|
| `SR203,204.stp` (oryginał) | **3** | **4, 5, 6, 7** |
| nasz SR213 | DSL=3, ADSL=6 | **4, 5, 6, 7** |
| nasz SR203_SR204 | 5 | 1, 2, 3, 4 |
| nasz ER115 | 5 | 1, 2 |
| nasz SE210 | — | 5 |

Oryginalny program i nasz własny profil SR213 używają **tego samego
układu**: kanał 3 dla Modem/DSL, kanały 4–7 dla Ethernet 1–4. Profil
SR203_SR204 jako jedyny używa kanałów 1–4 + 5.

Jeśli oprawka na stanowisku serwisowym jest okablowana tak samo jak przy
SR213 i przy programie oryginalnym, to **testowaliśmy niewłaściwe kanały** —
a wtedy prąd 0,34–0,41 mA, który widać w logach, pochodzi z pojemności
okablowania, nie z badanego portu. Przy 1,06 kV i 60 Hz 0,35 mA odpowiada
około 880 pF, co jest wartością osiągalną dla samej wiązki fixture.

Tego nie da się rozstrzygnąć z logów. **Rozstrzyga to jeden test:**

> Zamknij klapę z **pustą oprawką** (bez wyrobu) i uruchom test.
>
> * prąd spada do zera i leci Low Fail → kanały są dobrze podłączone,
> * prąd dalej ~0,35 mA i leci PASS → kanały są złe i wyniki z września
>   nie mówią nic o wyrobach.

Do czasu rozstrzygnięcia obniżenie Low Limit do 0,035 mA **osłabia** kontrolę
pustej oprawki — bo 0,035 mA przejdzie tym łatwiej. Dlatego ten test trzeba
zrobić przed produkcją, a nie po.

## Do sprawdzenia tą samą drogą

Profile **ER115**, **SE210** i **SR213** nie były porównane ze swoimi
zatwierdzonymi programami `.stp`. SE210 ma Low Limit 0,200 mA — tę samą
wartość, która okazała się błędna w SR203/SR204.
