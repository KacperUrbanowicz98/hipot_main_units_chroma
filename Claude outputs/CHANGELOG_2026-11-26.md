# Zmiany — 26.11.2026

## 1. Nowy profil SR213_2

Wariant SR213 z testem czterech portów Ethernet. Oryginalny **SR213
zostaje bez zmian** — to osobny profil, nie podmiana.

| # | krok | napięcie | High | Low | Obecność | kanał |
|---|------|---------:|-----:|----:|---------:|------:|
| 1 | ADSL | 1,50 kV | 1,500 mA | 0,020 mA | 0,020 mA | 2 |
| 2 | DSL | 1,50 kV | 1,500 mA | 0,020 mA | 0,020 mA | 3 |
| 3 | Ethernet 1 | 1,06 kV | 1,000 mA | 0,035 mA | 0,035 mA | 4 |
| 4 | Ethernet 2 | 1,06 kV | 1,000 mA | 0,035 mA | 0,035 mA | 5 |
| 5 | Ethernet 3 | 1,06 kV | 1,000 mA | 0,035 mA | 0,035 mA | 6 |
| 6 | Ethernet 4 | 1,06 kV | 1,000 mA | 0,035 mA | 0,035 mA | 7 |

ADSL i DSL mają nastawy z SR213, Ethernety — z SR203/SR204 (te same,
które po poprawce z 02.10 dały pierwsze prawdziwe FAIL-e). Czas cyklu
**14,5 s**, S/N 14 znaków, kanały 1 i 8 wolne.

Żaden kanał nie obsługuje dwóch kroków — inaczej niż w pierwotnym SR213,
gdzie `ADSL` i `Ethernet 3` miały wpisany ten sam kanał 6. Profil nie
zgłasza żadnego ostrzeżenia.

**Dwie rzeczy do zrobienia na stanowisku:**

1. Profil jest wyłączony. W `station_config.json` stoi
   `ENABLED_PRODUCTS: ["SR203_SR204"]`, więc SR213_2 nie pojawi się na
   liście operatora, dopóki nie zaznaczysz go w panelu inżynieryjnym
   (zakładka **Profile** → *Aktywne na tym stanowisku* → **Zapisz zestaw
   profili**).
2. Przypisanie kanałów mam wyłącznie od Ciebie — nie ma go w żadnym
   `.stp` ani w tabeli profili. Przed produkcją zrób jeden przebieg na
   **pustej oprawce**: każdy krok musi skończyć się poniżej progu
   obecności. Jeśli któryś pokaże prąd, ten kanał mierzy co innego, niż
   myślimy.

## 2. Interfejs dopasowuje się do rozdzielczości

Okno główne było maksymalizowane od zawsze, więc problem nie leżał
w oknie — leżał w **treści**. Rozmiary czcionek i odstępów były wpisane
na sztywno i policzone dla dużego monitora. Ekran testowy profilu
SR213_2 potrzebuje **900 px** wysokości; na monitorze 1366×768 po
odjęciu paska zadań zostaje **678 px**. Reszta, czyli STOP, stan klapy
i stopka, leżała pod krawędzią ekranu. Maksymalizacja okna tego nie
naprawiała: okno było duże, treść była od niego wyższa.

Teraz wszystkie rozmiary przechodzą przez jeden współczynnik liczony
z rozdzielczości. Ale sam współczynnik to za mało — część wysokości
(obramowania, grubości ramek, zaokrąglenia wierszy tabeli) nie zmienia
się razem z czcionką, więc ekran, który „powinien" zająć 678 px,
zajmował 752. Dlatego aplikacja po zbudowaniu ekranu **mierzy, ile
naprawdę zajął**, i poprawia współczynnik. Powtarza to najwyżej cztery
razy i zawsze **przed otwarciem portów** — przebudowa ekranu przy
połączonym testerze byłaby zmianą stanu sprzętu.

Wynik pomiaru dla profilu SR213_2 (najwyższa tabela kroków):

| ekran | dostępne | skala | ekran testu | |
|-------|---------:|------:|------------:|---|
| 1920×1080 | 990 px | **1,00** | 900 px | bez zmian |
| 1600×900 | 810 px | 0,83 | 783 px | |
| 1536×864 | 774 px | 0,80 | 771 px | |
| 1366×768 | 678 px | 0,64 | 677 px | |
| 1024×768 | 678 px | 0,64 | 677 px | |
| 1280×720 | 630 px | 0,62 | 605 px | bez panelu historii |

**Na dużym monitorze nie zmienia się nic.** Współczynnik ma twardy sufit
1,0, a przy 1,0 obie funkcje przepuszczają wartości bez zmian —
`ui.font(21, "bold")` zwraca dokładnie `("Arial", 21, "bold")`, a
`ui.px(68)` zwraca 68. Jest na to osobny test.

Dolna granica to **0,62**. Niżej napisy przestają być czytelne
z odległości, z jakiej operator patrzy na stanowisko. Na 1280×720 nawet
to nie wystarcza, więc aplikacja zabiera to, co ma najmniejszą wartość
operacyjną — panel **„Ostatnie wyniki"**. Tabela kroków, pomiary na
żywo, werdykt, stan klapy i STOP zostają. W logu pojawia się wtedy
wpis; do raportu wyniki idą normalnie, to był tylko podgląd na ekranie.

Skalowanie obejmuje też panel inżynieryjny, więc cała aplikacja wygląda
spójnie.

## Testy

**42 testy regresyjne.** Nowe:

* `test_ui_scale_is_neutral_at_full_size` — przy skali 1,0 nic się nie
  zmienia, skala nigdy nie przekracza 1,0 ani nie schodzi poniżej
  granicy czytelności, i w ekranach operatora nie została żadna czcionka
  wpisana na sztywno.
* `test_operator_screens_fit_small_station_monitors` — buduje **prawdziwy**
  ekran startowy i ekran testowy na sześciu rozdzielczościach i mierzy
  wysokość gotowego układu. Sprawdza też, że dopasowanie kończy się przed
  otwarciem portu testera.

Kontrola odwrotna: po wyłączeniu skalowania test upada już na 1600×900
(„ekran testowy potrzebuje 821 px, jest 810") — czyli problem dotyczył
nie tylko małych monitorów.

`test_profile_catalog` dostał SR213_2 z wypisanymi wprost maskami
kanałów. Maska to jedyna rzecz w tym profilu, której nie da się
wyprowadzić z żadnego dokumentu, a pomyłka w niej oznacza test wykonany
na niewłaściwej parze zacisków — czyli PASS, który niczego nie dowodzi.

## Przy okazji: dziura we własnych kontrolach buildera

Dopisując markery do `create_exe.py` wpisałem drugi klucz
`"test_screen.py"` do `REQUIRED_SAFETY_MARKERS`. Python zostawia
w takiej sytuacji ostatni wpis, więc **wszystkie markery bezpieczeństwa
tego pliku po cichu przepadły** — builder dalej przechodził. Złapałem to
sam, ale tylko dlatego, że akurat sprawdzałem słownik.

`test_builder_guards_catch_real_damage` sprawdza teraz, że żaden słownik
markerów nie ma powtórzonego klucza. Przy okazji `station_config.py`
przestał być plikiem bez markerów — mieszka tam teraz skala interfejsu.
