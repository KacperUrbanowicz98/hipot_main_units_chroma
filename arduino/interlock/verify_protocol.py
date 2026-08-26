"""Symulacja logiki interlock.ino i sprawdzenie jej wobec parsera aplikacji."""
import sys; sys.path.insert(0, "/home/claude/work/hipot_universal")
from interlock import InterlockMonitor

DEBOUNCE_MS, HEARTBEAT_MS, SAMPLE_MS = 40, 100, 5

def run(pin_closed_at_ms, total_ms):
    """Wierne odwzorowanie setup()+loop() ze szkicu. Zwraca (ms, linia)."""
    out = []
    stable = pin_closed_at_ms(0); cand = stable
    last_sample = cand_since = last_send = 0
    out.append((0, "CLOSED" if stable else "OPEN"))
    for now in range(0, total_ms + 1):
        if now - last_sample >= SAMPLE_MS:
            last_sample = now
            reading = pin_closed_at_ms(now)
            if reading != cand:
                cand, cand_since = reading, now
            elif cand != stable and now - cand_since >= DEBOUNCE_MS:
                stable = cand
                out.append((now, "CLOSED" if stable else "OPEN")); last_send = now
        if now - last_send >= HEARTBEAT_MS:
            out.append((now, "CLOSED" if stable else "OPEN")); last_send = now
    return out

# Styk drgajacy: klapa zamykana w 500 ms, 6 odbic po ~8 ms.
BOUNCE = [(500, True), (508, False), (514, True), (521, False),
          (527, True), (533, False), (539, True)]
def pin(now):
    state = False
    for t, s in BOUNCE:
        if now >= t: state = s
    return state

lines = run(pin, 1500)

# 1) Czy aplikacja w ogole rozumie te ramki?
mon = InterlockMonitor.__new__(InterlockMonitor)
mon._rx_buffer = bytearray(); mon.MAX_LINE_BYTES = 64
payload = "".join(txt + "\r\n" for _, txt in lines).encode()
parsed = mon._extract_lines(payload)
unknown = [m for m in parsed if m not in {"OPEN", "CLOSED"}]
print(f"1) ramek wyslanych: {len(lines)}, zrozumianych: {len(parsed)}, nieznanych: {len(unknown)}")
assert not unknown and len(parsed) == len(lines)

# 2) Ile przejsc stanu zobaczy aplikacja mimo 6 odbic styku?
trans = [(t, s) for i, (t, s) in enumerate(lines) if i == 0 or s != lines[i-1][1]]
print(f"2) odbic styku: {len(BOUNCE)-1}, przejsc zgloszonych: {len(trans)-1} -> {trans}")
assert len(trans) - 1 == 1, "debounce przepuscil falszywe przejscie"

# 3) Najwieksza przerwa miedzy ramkami vs. timeout aplikacji (2.0 s)
gaps = [b[0]-a[0] for a, b in zip(lines, lines[1:])]
print(f"3) max przerwa: {max(gaps)} ms, timeout aplikacji: 2000 ms -> zapas x{2000/max(gaps):.0f}")
assert max(gaps) < 2000

# 4) Opoznienie zgloszenia zamkniecia od ostatniego odbicia
print(f"4) CLOSED zgloszone {trans[1][0]-BOUNCE[-1][0]} ms po ustabilizowaniu styku")
print("\nOK - szkic zgodny z protokolem aplikacji")
