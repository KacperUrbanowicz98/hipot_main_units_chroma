/*
 * interlock.ino - Reconext Hi-Pot Main Units
 * Plyta: Arduino Pro Micro (ATmega32U4, USB natywny)
 *
 * Zadanie: raportowac stan klapy do aplikacji przez port szeregowy.
 *
 * Okablowanie:
 *   krancowka:  PIN 6 --- styk --- GND
 *   PIN 6 ma wlaczony INPUT_PULLUP, wiec:
 *     LOW  = styk zwarty  = klapa ZAMKNIETA -> "CLOSED"
 *     HIGH = styk rozwarty= klapa OTWARTA   -> "OPEN"
 *
 *   Przerwany kabel czyta sie jako HIGH, czyli OPEN. Awaria okablowania
 *   daje wiec stan bezpieczny - aplikacja zablokuje start testu.
 *
 * Protokol (musi sie zgadzac z interlock.py w aplikacji):
 *   - 9600 8N1
 *   - dokladnie dwa komunikaty: "OPEN" i "CLOSED", kazdy zakonczony nowa linia
 *   - komunikat wysylany co najmniej raz na 100 ms (heartbeat)
 *     Aplikacja wymaga poprawnego komunikatu co 2 s; brak = utrata interlocka
 *     i zablokowanie testu. 100 ms daje 20-krotny zapas.
 *   - zmiana stanu wysylana natychmiast, nie czeka na kolejny heartbeat
 *   - dodatkowo raz na sekunde linia "ID:<podpis stanowiska>", ktora
 *     aplikacja porownuje z INTERLOCK_IDENTITY w station_config.json
 *   - zadnych innych napisow, banerow ani logow - aplikacja odrzuca wszystko,
 *     co nie jest "OPEN", "CLOSED" ani "ID:..."
 *
 * UWAGA - biblioteka Keyboard zostala USUNIETA celowo.
 * Poprzednia wersja emulowala klawiature i wysylala Enter do komputera.
 * Plyta wpieta w stanowisko nie moze wstrzykiwac znakow do systemu:
 * "Enter" trafialby w dowolne aktywne okno, takze w okno potwierdzenia
 * czegokolwiek innego niz test.
 */

const uint8_t INTERLOCK_PIN = 6;

// Podpis stanowiska. Aplikacja porownuje go z INTERLOCK_IDENTITY
// w station_config.json i przerywa prace, gdy sie nie zgadza.
// To NIE jest zabezpieczenie kryptograficzne - podpis stoi tu otwartym
// tekstem. Wyklucza natomiast przypadkowe podlaczenie sie pod zly port COM
// i wymaga swiadomego dzialania, zeby go podrobic.
// Zostaw pusty ciag, jesli nie uzywasz tej kontroli.
const char STATION_SIGNATURE[] = "HIPOT-SR203-01";

// Co ile wysylac podpis.
const uint16_t IDENTITY_MS = 1000;

// Krancowka mechaniczna drga przy przelaczaniu. Stan musi byc stabilny
// przez tyle milisekund, zanim uznamy go za nowy stan klapy.
// Bez tego pojedyncze drgniecie moze udawac cykl OPEN -> CLOSED, a wlasnie
// na tym przejsciu aplikacja odblokowuje start testu.
const uint16_t DEBOUNCE_MS = 40;

// Co ile powtarzac stan, gdy nic sie nie zmienia (heartbeat dla aplikacji).
const uint16_t HEARTBEAT_MS = 100;

// Co ile probkowac wejscie.
const uint8_t SAMPLE_MS = 5;

// true = klapa zamknieta. Start od stanu bezpiecznego: dopoki nie zbierzemy
// stabilnego odczytu, raportujemy OPEN.
bool stableClosed = false;
bool candidateClosed = false;

unsigned long lastSampleMs = 0;
unsigned long candidateSinceMs = 0;
unsigned long lastSendMs = 0;
unsigned long lastIdentityMs = 0;

void sendIdentity() {
    if (STATION_SIGNATURE[0] == '\0') {
        return;
    }
    Serial.print("ID:");
    Serial.println(STATION_SIGNATURE);
    lastIdentityMs = millis();
}

void sendState(bool closed) {
    Serial.println(closed ? "CLOSED" : "OPEN");
    lastSendMs = millis();
}

void setup() {
    pinMode(INTERLOCK_PIN, INPUT_PULLUP);

    // Na ATmega32U4 predkosc jest ignorowana (USB CDC), ale podajemy ja
    // zgodnie z konfiguracja aplikacji.
    Serial.begin(9600);

    // Celowo BEZ "while (!Serial) {}". Na plycie z USB natywnym ta petla
    // czeka w nieskonczonosc, dopoki ktos nie otworzy portu - stanowisko
    // po restarcie zasilania stalo by z martwym interlockiem.

    unsigned long now = millis();
    lastSampleMs = now;
    candidateSinceMs = now;

    lastIdentityMs = now;

    stableClosed = (digitalRead(INTERLOCK_PIN) == LOW);
    candidateClosed = stableClosed;
    sendIdentity();
    sendState(stableClosed);
}

void loop() {
    unsigned long now = millis();

    // --- probkowanie z odfiltrowaniem drgan styku ---
    // Odejmowanie unsigned long jest odporne na przepelnienie millis()
    // (~49 dni), wiec plyta moze chodzic bez restartu.
    if (now - lastSampleMs >= SAMPLE_MS) {
        lastSampleMs = now;
        bool reading = (digitalRead(INTERLOCK_PIN) == LOW);

        if (reading != candidateClosed) {
            candidateClosed = reading;
            candidateSinceMs = now;
        } else if (candidateClosed != stableClosed &&
                   now - candidateSinceMs >= DEBOUNCE_MS) {
            stableClosed = candidateClosed;
            sendState(stableClosed);   // zmiana idzie natychmiast
        }
    }

    // --- heartbeat: powtorzenie stanu, gdy nic sie nie dzieje ---
    if (now - lastSendMs >= HEARTBEAT_MS) {
        sendState(stableClosed);
    }

    // --- podpis stanowiska, raz na sekunde ---
    if (now - lastIdentityMs >= IDENTITY_MS) {
        sendIdentity();
    }
}
