# Zmiany — 15.10.2026

## SR213 bez testu portów Ethernet

Na życzenie technologa z profilu **SR213** usunięte zostały kroki
**Ethernet 1–4**. Zostają dwa kroki:

| # | krok | napięcie | High | Low | kanał |
|---|---|---:|---:|---:|---|
| 1 | ADSL | 1,50 kV | 1,500 mA | 0,020 mA | 6 |
| 2 | DSL | 1,50 kV | 1,500 mA | 0,020 mA | 3 |

Czas cyklu spada z **14,5 s do 4,5 s**.

Zmieniony plik: `products/SR213.json`. Pozostałe profile bez zmian.

## Skutek uboczny: znikła kolizja kanału 6

Profil ostrzegał od początku, że krok **Ethernet 3** i krok **ADSL** mają
przypisany **ten sam kanał 6**. Ostrzeżenie zgłaszał mechanizm
`channel_warnings` — nie blokowało testu, bo oprawka teoretycznie może być
tak zrobiona, ale było typowym objawem pomyłki przy przepisywaniu maski.

Po usunięciu Ethernetów kolizji nie ma. **To jej nie wyjaśnia** — jeśli
kiedyś Ethernety wrócą do tego profilu, pytanie wróci razem z nimi
i wtedy trzeba będzie ustalić, który z tych dwóch kroków miał kanał 6
naprawdę.

## Co jeszcze poprawione

Dwa testy regresyjne pilnowały starego kształtu profilu:

* `test_profile_catalog` wymagał sześciu kroków i obecności ostrzeżenia
  o kanale 6. Teraz sprawdza odwrotnie: że Ethernetów nie ma, że zostały
  dokładnie `ADSL` i `DSL`, że oba idą po 1,5 kV i że ostrzeżeń nie ma.
* `test_profile_choice_is_explicit` miał wpisane na sztywno `1.06 kV`
  i `1.50 kV` w pasku profilu. Teraz liczy oczekiwane napięcia z samego
  profilu, więc przeżyje kolejną zmianę zestawu kroków.

Manifest sum kontrolnych profili przeliczony. **39 testów przechodzi.**

## Uwaga

Jeśli SR213 był już testowany na starym profilu, raporty z tamtych sztuk
mają pięć kroków więcej niż raporty z nowego. Przy porównywaniu wyników
w bazie to różnica widoczna w polu `STEP:`.
