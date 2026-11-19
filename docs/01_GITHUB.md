# 1. Projekt na GitHubie

Repo jest **publiczne** (https://github.com/panPianka/multiconnect), licencja **GPL-3.0-or-later** (`LICENSE`).
Każdy widzi kod i releasy, ale pushować mogą tylko collaboratorzy. W kodzie nie ma haseł –
dlatego tabela niżej jest jeszcze ważniejsza: wszystko, co trafi na GitHuba, jest od razu widoczne dla świata.

## Co trafia do repo, a co nigdy

Trafia: kod (`multiconnect/`), `config/providers/*.yaml`, `config/*.example.yaml`, prompty, szablony, testy, `.bat`, dokumentacja.

**Nigdy** (pilnuje `.gitignore`, nie usuwaj z niego tych linii):

| Ścieżka | Dlaczego |
|---|---|
| `state/` | ciasteczka logowania wszystkich kont (`state/secrets/`), baza |
| `config/commander.yaml`, `config/worker.yaml`, `config/accounts.yaml` | token zespołu, lista kont |
| `agents/`, `mirror_backups/` | foldery agentów, kopie zapasowe z loginami |
| `.venv/`, `.tools/`, `.browsers/`, `.godot_bin/`, `offline/` | duże narzędzia, idą w releasie offline |
| `dist/` | zbudowane zipy |

Przed każdym commitem: `git status` – jeśli widzisz coś z tabeli wyżej, **stop**.
Jeśli sekret jednak trafił na GitHuba: usunięcie pliku nie wystarczy (zostaje w historii) – wyloguj te konta
na stronach czatów (unieważnia ciasteczka) i zmień `team_token`.

## Sposób A – automatycznie: `github_setup.bat` (polecany)

Działa bez admina. Po kolei:
1. pobiera GitHub CLI (`gh`) do `.tools\gh`, jeśli go nie ma;
2. **logowanie**: pokaże 8-znakowy kod i otworzy przeglądarkę → wklej kod → zatwierdź;
3. pyta o imię i e-mail do commitów (tylko dla tego repo). Prywatny e-mail ukryjesz adresem
   `ID+login@users.noreply.github.com` z GitHub → Settings → Emails;
4. robi pierwszy commit na gałęzi `main` (pokazuje listę plików – sprawdź ją!);
5. tworzy prywatne repo `multiconnect` (inna nazwa: `github_setup.bat moja-nazwa`) i wypycha kod.

Na końcu wypisuje adres repo. Upublicznienie (zrobione dla naszego repo):
```bash
gh repo edit --visibility public --accept-visibility-change-consequences
```
albo GitHub → repo → **Settings → General → Danger Zone → Change visibility**.

## Sposób B – ręcznie (gdy skrypt nie działa)

1. https://github.com/new → nazwa `multiconnect` → **Private** → **bez** README/.gitignore/licencji → Create.
2. W folderze narzędzia (Git Bash albo cmd):
```bash
git config user.name "Imię Nazwisko"
```
```bash
git config user.email "twoj-noreply@users.noreply.github.com"
```
```bash
git checkout -B main
```
```bash
git add -A
```
```bash
git status
```
```bash
git commit -m "MultiConnect: initial version"
```
```bash
git remote add origin https://github.com/TWOJ_LOGIN/multiconnect.git
```
```bash
git push -u origin main
```
Przy pushu Git for Windows otworzy okno logowania do GitHuba (Git Credential Manager).

## Zespół

GitHub → repo → **Settings → Collaborators → Add people** → login kolegi. Musi zaakceptować zaproszenie (mail).

## Na innym komputerze

- Z gitem: `git clone https://github.com/TWOJ_LOGIN/multiconnect.git D:\multiconnect` → `setup.bat`.
- Bez gita / bez internetu: pobierz zip z **Releases** (patrz 02_RELEASE.md) albo z pendrive'a.
- Aktualizacja: `git pull` w folderze narzędzia, potem zrestartuj `commander.bat`/`worker.bat`
  (`_ensure_py.bat` sam doinstaluje nowe paczki, jeśli trzeba).

## Codzienna praca

```bash
git pull
```
```bash
git add -A
```
```bash
git commit -m "co zmieniłem"
```
```bash
git push
```
Po każdym pushu na `main` GitHub sam uruchamia testy (zakładka **Actions**, workflow `tests`). Zielone = OK.

## Gra a repo narzędzia

Folder gry (`game_dir`) jest **osobny** i ma własny lokalny git (commit przy każdej integracji).
Jeśli chcecie grę też na GitHubie: w folderze gry `gh repo create nazwa-gry --private --source . --push`
(albo sposób B). Nie wrzucaj folderu gry do repo narzędzia.
