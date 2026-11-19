# MultiConnect

Claude (albo Codex) dowodzi przez MCP dziesiątkami czatów AI w przeglądarkach (ChatGPT, DeepSeek, Gemini, Mistral, Qwen…)
na wielu komputerach w szkolnej sieci LAN. Każdy czat staje się **agentem** z własnym folderem i sandboksem.
Agent pisze kod gry w Godocie, sprawdza go headless i odsyła raport. Claude czyta raporty i składa grę.

```
 [PC dowódcy]  Claude Code ──MCP──► koordynator (panel :8765, SQLite) ◄──WebSocket── worker (PC 2..N)
                                         │                                          └ 1 Chromium, N kont = N agentów
                                         └ folder gry (git + backupy zip)
```

> **Na hackathon: najpierw [RUNBOOK.md](RUNBOOK.md)** – checklisty i plany awaryjne na każdą awarię.
> Instalacja, konfiguracja, GitHub i release: **[docs/](docs/README.md)**.

## 1. Instalacja (każdy komputer, bez admina)

1. Skopiuj folder narzędzia (albo `git clone`) gdziekolwiek, np. `D:\multiconnect`.
2. Uruchom **`setup.bat`**. Instaluje do środka folderu: uv, Pythona 3.12, paczki, Chromium i Godota (wersja konsolowa).
   Na końcu odpala kreator:
   - **komputer dowódcy** → wybierz `commander`. Generuje token zespołu, ustawia folder gry (POZA repo) i backupów.
   - **pozostałe komputery** → wybierz `worker` i wpisz ten sam token. Adres koordynatora można zostawić pusty,
     bo worker sam go znajdzie w LAN-ie (UDP 8766).
3. `doctor.bat` sprawdza, czy wszystko gra, a **`selftest.bat`** w 2–3 min przepuszcza na tym PC cały potok
   (koordynator, worker, przeglądarka, agenci na fałszywym czacie, Godot, integracja, backup). Ma pokazać `SELFTEST PASSED`.

**Bez internetu?** Na komputerze z netem uruchom `make_offline_bundle.bat`, potem skopiuj cały folder
(z `.tools`, `.browsers`, `offline`) na pendrive. Na docelowym PC wystarczy `setup.bat`.
**Brak Chromium?** W `config/worker.yaml` ustaw `browser_channel: msedge`, wtedy używany jest wbudowany Edge.

## 2. Konta

Plik `config/accounts.yaml` (wzór: `accounts.example.yaml`). **Bez haseł**: każde konto loguje się raz, ręcznie.

```yaml
accounts:
  - {id: anna-gpt, provider: chatgpt, owner: Anna}
  - {id: anna-ds,  provider: deepseek, owner: Anna}
```

Logowanie: w panelu kliknij agenta i wybierz **Zaloguj**. Na wybranym PC otworzy się małe okno. Zaloguj się w nim
i je zamknij (albo poczekaj, aż samo wykryje zalogowanie). Ciasteczka trafiają do `state/secrets/` na PC dowódcy
i każdy worker je dostaje, więc konto może działać na dowolnym komputerze. Alternatywa: `login.bat anna-gpt`.
Po zmianie pliku kliknij w panelu „reload accounts” albo zrestartuj koordynator.

## 3. Start

| Komputer | Co uruchomić |
|---|---|
| dowódcy | `commander.bat` (panel: `http://localhost:8765`, z innych PC: `http://IP:8765`) |
| dowódcy, jednorazowo | `connect_claude.bat` (rejestruje MCP w Claude Code i robi snippet dla Codexa) |
| każdy (też dowódcy) | `worker.bat` |

Potem otwórz **Claude Code w folderze gry** (np. `F:\hackathon-game`). Folder ma `CLAUDE.md` z instrukcją
dowodzenia, więc wystarczy napisać po polsku, jaka ma być gra. Claude ustala kontrakt, rozdziela zadania,
zamawia assety i scala wyniki.

## 4. Panel

- **Komputery**: online/offline, RAM/CPU, przyciski **+/−** z liczbą agentów na danym PC (≈0,4 GB RAM na agenta),
  tryb okien (`mini` / `hidden` / `headless`), restart.
- **Agenci**: kolor oznacza stan (niebieski: pracuje, zielony: bezczynny, żółty: cooldown, czerwony: błąd, logowanie
  albo CAPTCHA). Licznik bezczynności robi się żółty po 5 min i czerwony po 15 min. Kliknięcie otwiera szczegóły:
  zrzut ekranu, pokaż okno, odśwież, zaloguj, transkrypt, zdarzenia i podpowiedź naprawy.
- **Zadania**: kanban i graf zależności, szczegóły, raport, „daj innemu agentowi”. Bez Claude też się da:
  **+ Nowe zadanie**, **Kontrakt**, **Sprawdź całą grę**, a w zadaniu **Zintegruj** i **Poprawka dla agenta**.
- **Assety**: lista zamówień od Claude. Kliknij „Zrobię to”, a potem wgraj plik albo przeciągnij go na pole upuszczania.
  Do tego czasu w grze jest kolorowa zaślepka.
- **Tokeny**: szacunek łącznie, na agenta, dostawcę i komputer oraz wykres na minutę.
- **Dostawcy**: włączanie i wyłączanie, a także **tester selektorów**. Kiedy strona czatu zmieni wygląd, podświetlasz
  elementy w oknie agenta, poprawiasz selektor i klikasz Zapisz. Zmiana trafia od razu na wszystkie PC.
- **Gra i backupy**: check w Godocie, drzewo plików, backup teraz / przywróć, zip z diagnostyką.
- **Zdarzenia**: log z filtrami i podpowiedziami do kodów błędów (zakładka Pomoc).
- Przełącznik **EN/PL**, tryb ciemny, dźwięki, „Wstrzymaj wszystko”.

## 5. Dostawcy

Pliki `config/providers/*.yaml`. Domyślnie włączone są: chatgpt, deepseek, gemini, mistral, qwen i mock.
Wyłączone, ale gotowe: claude.ai, copilot, grok, kimi, z.ai, huggingchat i perplexity. Włączasz je w panelu.
Selektory to najlepsze przybliżenie, więc **przed hackathonem przetestuj każdego dostawcę** (tester selektorów).
Nowy czat = nowy plik YAML (skopiuj np. `qwen.yaml`).
**Plan B: `manual`** – człowiek kopiuje wiadomości agenta do dowolnego czatu AI i wkleja odpowiedzi
na `http://127.0.0.1:8790/manual/inbox` (szczegóły w RUNBOOK F.5).

## 6. Test bez prawdziwych kont

Najprościej: `selftest.bat` (albo `selftest.bat --manual` dla ręcznego przekaźnika). Ręcznie:

```
mockchat.bat            # fałszywy czat na http://127.0.0.1:8790
```
W `accounts.yaml` dodaj `- {id: mock1, provider: mock}` (i kilka kolejnych), uruchom `commander.bat` i `worker.bat`,
zaloguj mocki (przycisk „Log in” na stronie mocka) i każ Claude coś zlecić.

## 7. Bezpieczeństwo i backupy

- `state/secrets/` zawiera ciasteczka logowania. Nie wrzucaj go nigdzie (jest w `.gitignore`).
- Backup zip folderu gry robi się co 10 min (tylko gdy coś się zmieniło), a do tego git commit przy każdej integracji.
  `mirror_backups_to` kopiuje backupy na inne PC.
- Co 10 min także `hive-*.zip` (baza, loginy, config) – po awarii PC dowódcy `restore_hive.bat --game` na innym PC
  przenosi dowódcę (RUNBOOK F.6). Zawiera ciasteczka, więc tylko na naszych PC.
- Sieć nie działa? `netcheck.bat`, `tunnel.bat` (tunel przez internet), `solo.bat` (wszystko na jednym PC).
- `_hive/HANDOFF.md` w folderze gry pozwala innemu Claude / Codexowi przejąć dowodzenie w każdej chwili.

## Licencja

MultiConnect jest udostępniany na licencji **GNU GPL v3.0 lub nowszej** (`GPL-3.0-or-later`) – pełny tekst w [LICENSE](LICENSE).
Możesz używać, zmieniać i rozpowszechniać kod, ale rozpowszechniane wersje (także zmienione) muszą mieć otwarty
kod na tej samej licencji. Zip offline zawiera też narzędzia innych autorów na ich własnych licencjach
(Python, Chromium/Playwright, Godot, uv, cloudflared).

MultiConnect is licensed under the **GNU General Public License v3.0 or later** – see [LICENSE](LICENSE).
