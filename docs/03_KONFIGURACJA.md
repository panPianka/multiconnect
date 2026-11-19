# 3. Co i gdzie ustawić

Wszystko ustawia kreator w `setup.bat` (krok 5). Ten plik mówi, co znaczą pola i co zmieniać ręcznie.
Pliki `config\*.yaml` są lokalne dla każdego PC i **nie idą do gita** – wzory to `config\*.example.yaml`.

## Rola komputerów

| PC | Pliki | Uruchamia |
|---|---|---|
| dowódca (1 sztuka) | `config\commander.yaml`, `config\accounts.yaml` (+ `worker.yaml`, bo też ma agentów) | `commander.bat`, `worker.bat`, Claude Code |
| pozostałe | `config\worker.yaml` | `worker.bat` |

## `config\commander.yaml` (dowódca)

| Pole | Ustaw | Uwagi |
|---|---|---|
| `team_token` | losowy (kreator generuje) | **ten sam** na wszystkich workerach; chroni panel i API |
| `port` | `8765` | zmień tylko gdy zajęty; wtedy też `coordinator_port` u workerów |
| `game_dir` | np. `D:/hackathon-game` | **poza** folderem narzędzia; tu powstaje gra (Godot) |
| `backup_dir` | np. `D:/hackathon-backups` | zipy gry i `hive-*.zip`; najlepiej inny dysk niż gra |
| `backup_every_min` | `10` | `0` = wyłącza auto-backup gry |
| `hive_backup_every_min` | `10` (domyślnie) | kopia bazy + loginów do przeniesienia dowódcy |
| `mirror_backups_to` | `[]` = automatycznie 1 inny PC | albo lista nazw PC, np. `[PC-02, PC-03]` |
| `default_agents_per_host` | `6` | zmieniasz na żywo **+/−** w panelu (~0,4 GB RAM na agenta) |
| `task_timeout_min` | `25` | po tylu minutach bez postępu zadanie idzie do innego agenta |
| `disabled_providers` | `[]` | lepiej przełączać w panelu (zakładka Dostawcy) |

## `config\worker.yaml` (każdy PC)

| Pole | Ustaw | Uwagi |
|---|---|---|
| `team_token` | skopiuj z dowódcy | zły token = „401” w oknie workera |
| `coordinator_url` | puste | puste = sam znajdzie (UDP, skan sieci). Wpisz `http://IP:8765` albo adres tunelu, gdy nie znajduje |
| `browser_mode` | `mini` | `hidden` / `headless` oszczędza ekran i RAM; `mini` pozwala patrzeć |
| `browser_channel` | `chromium` | `msedge` gdy Chromium nie działa (Edge jest w Windowsie) |
| `name` | puste | puste = nazwa komputera (widać ją w panelu, używa jej `pin_host`) |

## `config\accounts.yaml` (tylko dowódca)

```yaml
accounts:
  - {id: anna-gpt,  provider: chatgpt,  owner: Anna}
  - {id: anna-ds,   provider: deepseek, owner: Anna}
  - {id: kuba-gem,  provider: gemini,   owner: Kuba, pin_host: PC-KUBY}
  - {id: reczny1,   provider: manual,   owner: Kuba, pin_host: PC-KUBY}   # plan B: ręczny przekaźnik
```
- **Bez haseł.** Logowanie raz: panel → agent → **Zaloguj** (albo `login.bat anna-gpt`). Ciasteczka → `state\secrets\`.
- `id` krótkie i unikalne. `provider` = nazwa pliku z `config\providers\`.
- `pin_host` – konto zawsze na tym PC (np. bo właściciel tam rozwiązuje CAPTCHA).
- Po zmianie: panel → **Wczytaj accounts.yaml**.
- Ile kont? Każde konto = 1 agent naraz. Darmowe konta mają limity → im więcej różnych dostawców, tym lepiej.

## Dostawcy (`config\providers\*.yaml`)

Włączanie/wyłączanie i poprawki selektorów **w panelu** (zakładka Dostawcy) – zapis idzie do bazy i na wszystkie PC.
Edycja plików YAML tylko gdy dodajesz nowy czat (skopiuj np. `qwen.yaml`). `manual` = plan B (RUNBOOK F.5).

## Claude Code / Codex (dowódca)

1. `connect_claude.bat` – raz. Rejestruje serwer MCP `multiconnect` w Claude Code (`claude mcp list` pokaże go)
   i tworzy `codex_mcp_snippet.toml` dla Codexa (dopisz do `%USERPROFILE%\.codex\config.toml`).
2. Claude Code otwieraj **w folderze gry** (`game_dir`) – tam jest `CLAUDE.md` z instrukcją dowodzenia
   (Codex czyta `AGENTS.md`). Napisz po polsku, jaką grę robicie.
3. Zmiana konta Claude / przejście na Codex: „przeczytaj _hive/HANDOFF.md i przejmij dowodzenie”.

## Sprawdzenie, że wszystko ustawione

`doctor.bat` (config, Python, Chromium, Godot, sieć) → `selftest.bat` (cały potok) → panel `http://IP:8765`.
