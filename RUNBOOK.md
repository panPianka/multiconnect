# RUNBOOK – hackathon (plany awaryjne)

Zasada: **nic nie zakładamy, wszystko sprawdzamy rano**. Każda awaria ma tu gotową reakcję.
Jedna osoba to **dowódca** (PC z `commander.bat` + Claude Code), reszta pilnuje workerów i assetów.

---

## A. Dzień przed (w domu, z internetem)

- [ ] `setup.bat` na swoim laptopie/PC, potem **`selftest.bat`** → musi być `SELFTEST PASSED`.
- [ ] `selftest.bat --manual` → test planu B dla czatów (ręczny przekaźnik). Też PASSED.
- [ ] **`make_offline_bundle.bat`**: pobiera wszystko (uv, Python, paczki, Chromium, Godot, cloudflared) do folderu.
      Potem **`make_release.bat --pendrive`** → `dist\multiconnect-v0.1.0-offline.zip` (~600 MB, bez loginów i tokenów)
      na **2 pendrive'y** (różne osoby). Na PC rozpakuj do krótkiej ścieżki, np. `D:\mc`.
      Nie kopiuj ręcznie `state\` ani `config\accounts.yaml` (ciasteczka logowania).
- [ ] Konta: każdy zapisuje na kartce, **które konta** (ChatGPT, DeepSeek, Gemini, Mistral, Qwen…) ma i czy pamięta hasło.
      Minimum 2 różnych dostawców na osobę. Haseł NIE wpisujemy do żadnego pliku.
- [ ] Claude: kto ma konto Claude Code (dowódca) + **zapasowe konto** Claude albo Codex (patrz F.9).
- [ ] Wydrukuj / zapisz offline ten RUNBOOK.

## B. Rano na miejscu (kolejność ma znaczenie)

1. Na każdym PC: rozpakuj zip z pendrive'a do krótkiej ścieżki z prawem zapisu (np. `D:\mc`) → `multiconnect\setup.bat`.
   Działa bez admina i bez internetu (jeśli jest `offline\`).
2. Na każdym PC: **`selftest.bat`** (2–3 min). PASSED = Python, Chromium, Godot i cały potok działają na tym PC.
3. Wybór dowódcy – **sieć**:
   - na kandydacie: `netcheck.bat --listen` (zostaw otwarte),
   - na innym PC: `netcheck.bat <IP kandydata>` → wynik mówi wprost, co działa.
   - ✅ OK → ten PC zostaje dowódcą. ❌ firewall → sprawdź inny PC jako kandydata. Nic nie działa → F.1.
4. Dowódca: `setup.bat` (rola commander, zapisz **token zespołu**), `commander.bat`, `connect_claude.bat` (raz).
5. Pozostali: `setup.bat` (rola worker, ten sam token, adres pusty = sam znajdzie) → `worker.bat`.
6. Panel `http://IP_DOWÓDCY:8765` → zakładka Agenci → **Zaloguj** każde konto (raz, ręcznie).
7. Zakładka **Dostawcy → tester selektorów**: dla każdego włączonego dostawcy wyślij próbne zadanie
   albo podświetl pole tekstowe / przycisk wyślij. Coś nie pasuje → F.4.
8. Dowódca otwiera **Claude Code w folderze gry** i pisze po polsku, jaka ma być gra.

---

## C. Codzienne komendy

| Plik | Co robi |
|---|---|
| `commander.bat` | koordynator + panel (PC dowódcy) |
| `worker.bat` | agenci na tym PC (każdy PC, także dowódcy) |
| `selftest.bat` | pełny test na tym PC na fałszywym czacie (`--manual` = test ręcznego przekaźnika, `--mode mini` = widać okna) |
| `netcheck.bat` | diagnoza sieci/firewalla (`--listen` na dowódcy, `<IP>` na workerze) |
| `solo.bat` | dowódca + worker na jednym PC, bez sieci |
| `tunnel.bat` | tunel internetowy do dowódcy (gdy LAN blokuje) |
| `restore_hive.bat` | przeniesienie dowódcy na inny PC z kopii (`--game` = też gra) |
| `doctor.bat` | sprawdza instalację i konfigurację |
| `login.bat <konto>` | ręczne logowanie konta bez panelu |

---

## D. Kopie zapasowe – co jest gdzie

- **Gra**: git commit przy każdej integracji + zip `game-*.zip` co 10 min (w `backup_dir`), kopiowane na 1 inny PC
  do `mirror_backups\`. Przywracanie: panel → Gra i backupy → Przywróć (przed tym robi się kopia `pre-restore`).
- **Hub** (baza zadań, czatów, tokenów + ciasteczka logowania + config): `hive-*.zip` co 10 min, ostatnie 10,
  też kopiowane do `mirror_backups\` na innym PC. Przycisk „Backup teraz” w panelu robi obie kopie od razu.
- **Mózg dowódcy**: `_hive/HANDOFF.md` + `_hive/contract.md` w folderze gry – nowy Claude/Codex czyta je i przejmuje.
- `hive-*.zip` zawiera ciasteczka logowania → **nie wysyłać nigdzie poza nasze PC**.

---

## E. Szybka diagnoza: co widzę → co robię

| Objaw | Akcja |
|---|---|
| Worker pisze „commander not found” | F.1 (sieć). Szybki test: `netcheck.bat <IP dowódcy>` |
| Worker: „401 / bad team token” | inny token. Popraw `team_token` w `config\worker.yaml` (skopiuj z `config\commander.yaml` dowódcy) |
| Agent czerwony „logged out / login” | panel → agent → **Zaloguj** (albo `login.bat konto`) |
| Agent czerwony CAPTCHA | panel → agent → **Pokaż okno** → rozwiąż CAPTCHA ręcznie. Powtarza się → wyłącz konto, użyj innego dostawcy |
| Agent żółty (cooldown, rate limit) | nic – wraca sam. Dużo takich → włącz więcej kont / dostawców, zmniejsz `redundancy` |
| Zadania stoją w kolejce, agenci wolni | sprawdź zakładkę Zdarzenia (`fix.no_agent`): wszyscy wykluczeni/wyłączeni → włącz dostawcę lub dodaj agentów (+) |
| Strona czatu zmieniła wygląd, agent nie wysyła / nie czyta odpowiedzi | F.4 (selektory), w międzyczasie F.5 (ręczny przekaźnik) |
| „Godot not found” / check nie działa | `setup.bat` jeszcze raz (pobiera Godota do `.godot_bin`) albo wskaż `godot_path` w configu. Bez Godota zadania przechodzą, ale bez testu – sprawdzaj grę ręcznie |
| `commander.bat`/`worker.bat` od razu się zamyka / błąd importu | uruchom ponownie – `_ensure_py` naprawia `.venv` sam. Dalej źle → `setup.bat` |
| PC dowódcy padł / zawiesił się | F.6 |
| Claude skończył limit / nie odpowiada | F.9 |
| Gra zepsuta po integracji | panel → Gra i backupy → Przywróć ostatni dobry zip, albo `git log` / `git checkout` w folderze gry |
| Brak internetu na sali | Agenci potrzebują internetu (to czaty w przeglądarce). Hotspot z telefonu na 1–2 PC + `solo.bat` (F.3) |
| Mało RAM-u, PC muli | panel → Komputery → **−** (mniej agentów), tryb okien `headless` |

---

## F. Plany awaryjne

### F.1 Sieć – worker nie widzi dowódcy
Worker szuka sam: adres z configu → ostatni znany → UDP beacon → skan podsieci. Jeśli nic:
1. `netcheck.bat --listen` na dowódcy, `netcheck.bat <IP>` na workerze. Komunikat mówi, czy to firewall, czy brak serwera.
2. Okienko „Zapora Windows” przy pierwszym starcie `commander.bat`: kliknij **Zezwól** (sieć prywatna). Bez admina
   często nie da się – wtedy:
3. **Inny PC jako dowódca** – na niektórych firewall przepuszcza. Sprawdź netcheckiem 2–3 kandydatów.
4. Plan C – **tunel** (F.2). 5. Plan D – **solo** (F.3).
6. Gdy podsieć jest izolowana (Wi-Fi gościnne): wszystkie PC na ten sam kabel/switch albo F.2.

### F.2 Plan C – tunel przez internet (gdy LAN blokuje połączenia przychodzące)
Na PC dowódcy, obok `commander.bat`: `tunnel.bat` → pojawi się adres `https://xxxx.trycloudflare.com`.
Na każdym workerze (cmd w folderze narzędzia):
```
set MC_COORDINATOR=https://xxxx.trycloudflare.com
worker.bat
```
(albo wpisz go jako `coordinator_url` w `config\worker.yaml`). Wszystko dalej chroni token zespołu.
Adres zmienia się po każdym restarcie tunelu – wtedy podaj nowy. Panel też działa pod tym adresem.

### F.3 Plan D – solo (bez sieci między PC)
`solo.bat` na jednym PC = dowódca + worker + agenci tylko na nim. Mocny PC (16 GB) uciągnie ~8–12 agentów.
Wariant zespołowy: dwa niezależne „ula” na dwóch PC, każdy robi inną część gry, a wyniki łączymy przez pendrive
(kopiujemy pliki do jednego folderu gry; potem `godot_check`).

### F.4 Strona czatu zmieniła wygląd (selektory)
Panel → **Dostawcy → tester selektorów**: wybierz działającego agenta tego dostawcy, wpisz selektor CSS
(pole tekstowe, przycisk wyślij, wiadomość asystenta, blok kodu), kliknij Podświetl, aż trafi, potem **Zapisz**.
Zmiana idzie od razu na wszystkie PC (zapis w bazie, nie w plikach). Jeśli się nie da w 10 minut, **wyłącz** tego
dostawcę w panelu – zadania pójdą do innych.

### F.5 Plan B dla czatów – ręczny przekaźnik („jesteś przeglądarką”)
Gdy automat dla jakiejś strony nie działa (CAPTCHA, blokada, nowy wygląd) albo chcesz użyć AI bez pliku dostawcy:
1. `config\accounts.yaml` na dowódcy: `- {id: reczny1, provider: manual, owner: Kuba, pin_host: NAZWA-PC-KUBY}`
   → panel „Wczytaj accounts.yaml”.
2. Panel → Dostawcy → włącz **Manual relay**.
3. Na PC Kuby otwórz `http://127.0.0.1:8790/manual/inbox`. Każda wiadomość od agenta: **Copy message** →
   wklej do dowolnego czatu AI (jeden czat AI na jeden „Agent chat”), skopiuj odpowiedź przyciskiem **Copy**
   pod odpowiedzią czatu → wklej → **Submit answer**. Reszta (sandbox, test Godota, raport) działa normalnie.
4. Odpowiedź bez bloków ``` też przejdzie (naprawiamy ją automatycznie), ale przycisk Copy czatu jest pewniejszy.

### F.6 PC dowódcy padł – przeniesienie dowódcy
Na innym PC (najlepiej tym, który ma `mirror_backups\` z kopiami):
1. Zamknij tam `worker.bat`. Jeśli ten PC nie miał configu dowódcy, nic nie szkodzi – kopia go zawiera.
2. **`restore_hive.bat --game`** → bierze najnowszy `hive-*.zip` (baza + loginy + config) i `game-*.zip` (gra).
   Ścieżka `game_dir` z kopii może nie istnieć na tym PC – wtedy popraw ją w `config\commander.yaml`
   i uruchom `restore_hive.bat --game` jeszcze raz.
3. `commander.bat`, `worker.bat`, `connect_claude.bat`. Pozostałe workery znajdą nowego dowódcę same (skan podsieci;
   restart `worker.bat` przyspiesza). Przerwane zadania wracają do kolejki automatycznie.
4. Claude Code w nowym folderze gry: „przeczytaj _hive/HANDOFF.md i przejmij dowodzenie”.
Gdy PC dowódcy tylko się zawiesił, ale dysk jest cały: po restarcie zwyczajnie `commander.bat` – stan jest w SQLite.

### F.7 Worker padł
Nic nie trzeba robić: po ~20 s jego zadania wracają do kolejki i biorą je inni. Po restarcie PC → `worker.bat`.

### F.8 Panel nie działa / nie ma Claude – praca ręczna
Zakładka **Zadania**: **+ Nowe zadanie** (cel PO ANGIELSKU, pliki, interfejs, kryteria), **Kontrakt**, **Sprawdź całą grę**.
W szczegółach zadania: **Zintegruj z grą**, **Zintegruj mimo to**, **Poprawka dla agenta** (follow-up do tego samego czatu).
Czyli ul działa nawet wtedy, gdy Claude jest niedostępny – ludzie dowodzą z panelu.

### F.9 Claude: limit / brak dostępu
1. W Claude Code: „zrób snapshot i zaktualizuj next_steps” (o ile jeszcze odpowiada).
2. Zapasowe konto Claude **albo Codex**: `connect_claude.bat` zrobił `codex_mcp_snippet.toml` – dopisz go do configu
   Codexa, uruchom Codex w folderze gry (ma `AGENTS.md`) i napisz: „przeczytaj _hive/HANDOFF.md i przejmij”.
3. W międzyczasie: F.8 (panel ręcznie). Agenci nie potrzebują Claude, żeby kończyć już zlecone zadania.
4. Oszczędzanie limitu: każ Claude czytać raporty `short`, nie kod; większe zadania rzadziej; mniej `redundancy`.

### F.10 Brak Chromium / Playwright nie startuje
`config\worker.yaml`: `browser_channel: msedge` (Edge jest na każdym Windowsie). Albo `setup.bat` jeszcze raz.

### F.11 Nic nie działa z naszym narzędziem
Ostatnia deska: każdy pracuje w Godocie ręcznie z czatem AI w przeglądarce, kontrakt w `_hive/contract.md`
jest wspólną specyfikacją, a pliki zbieramy pendrivem do jednego folderu gry. Backupy gry są zwykłymi zipami.

---

## G. Bezpieczeństwo

- `state\secrets\` i `hive-*.zip` = ciasteczka logowania wszystkich kont. Tylko nasze PC. Po hackathonie usuń
  `state\`, `mirror_backups\` i backupy z PC szkolnych.
- Token zespołu nie jest hasłem do kont, ale chroni panel i tunel – nie wklejaj go publicznie.
- Selftest tworzy pliki w `state\selftest\` (sprząta je sam po sukcesie) – można je usuwać bez obaw.
