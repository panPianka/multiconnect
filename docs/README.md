# Dokumentacja MultiConnect – zacznij tutaj

| Plik | Kiedy czytać |
|---|---|
| [01_GITHUB.md](01_GITHUB.md) | wrzucenie projektu na GitHuba (raz) i dodanie zespołu |
| [02_RELEASE.md](02_RELEASE.md) | jak wydać wersję (zip do pobrania, także offline na pendrive) |
| [03_KONFIGURACJA.md](03_KONFIGURACJA.md) | co ustawić na każdym komputerze: token, konta, folder gry, Claude/Codex |
| [../RUNBOOK.md](../RUNBOOK.md) | **na hackathon**: checklisty i plany awaryjne |
| [../README.md](../README.md) | opis narzędzia i panelu |

## Najkrótsza ścieżka (dziś wieczorem)

1. `github_setup.bat` → loguje do GitHuba, robi pierwszy commit, tworzy repo i wypycha kod
   (zrobione: publiczne repo https://github.com/panPianka/multiconnect, licencja GPL-3.0-or-later).
2. GitHub → repo → **Settings → Collaborators** → dodaj osoby z zespołu.
3. `make_offline_bundle.bat` (jeśli jeszcze nie) → `make_release.bat --publish` → release z zipami.
4. Pendrive: `make_release.bat --pendrive` → `dist\multiconnect-v0.1.0-offline.zip` na pendrive.
   Na PC rozpakuj do krótkiej ścieżki (`D:\mc`) → `setup.bat`.
5. Jutro rano: RUNBOOK, sekcja B.

## Pliki .bat – ściąga

| Plik | Co robi |
|---|---|
| `setup.bat` | instalacja w folderze (bez admina) + kreator configu |
| `doctor.bat` | sprawdza instalację |
| `selftest.bat` | pełny test potoku na tym PC (fałszywy czat) |
| `commander.bat` / `worker.bat` | dowódca / agenci |
| `connect_claude.bat` | podpina MCP do Claude Code + snippet dla Codexa |
| `login.bat <konto>` | ręczne logowanie konta |
| `netcheck.bat`, `tunnel.bat`, `solo.bat` | sieć: diagnoza, tunel, tryb jednego PC |
| `restore_hive.bat` | przeniesienie dowódcy na inny PC |
| `mockchat.bat` | fałszywy czat do testów |
| `make_offline_bundle.bat` | pobiera wszystko do folderu (instalacja bez internetu) |
| `make_release.bat` | buduje zipy do wydania (`--publish` = od razu na GitHuba) |
| `github_setup.bat` | jednorazowe wrzucenie repo na GitHuba |
