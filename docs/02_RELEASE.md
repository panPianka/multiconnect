# 2. Release (wydanie wersji do pobrania)

Release to paczka zip przypięta do tagu wersji (np. `v0.1.0`) na GitHubie → zakładka **Releases**.
Dwa pliki:

| Plik | Rozmiar | Dla kogo |
|---|---|---|
| `multiconnect-v0.1.0-source.zip` | ~0,2 MB | PC z internetem: rozpakuj → `setup.bat` (pobierze resztę) |
| `multiconnect-v0.1.0-offline.zip` | kilkaset MB | PC **bez internetu**: w środku Python, paczki, Chromium, Godot, cloudflared, uv |

Oba zipy budowane są z **ostatniego commita** i sprawdzane, czy nie ma w nich `state/`, `secrets/`,
`config/*.yaml` z tokenem/kontami ani `hive-*.zip`. Jeśli coś takiego się znajdzie, zip jest kasowany.

## Krok po kroku

1. Wszystko zacommitowane i wypchnięte (`git status` czysty). Skrypt odmówi, jeśli nie.
2. Numer wersji: `version = "..."` w `pyproject.toml` (teraz `0.1.0`). Nowa wersja → zmień, potem:
```bash
uv lock
```
   i zacommituj `pyproject.toml` + `uv.lock`.
3. `make_offline_bundle.bat` – tylko jeśli chcesz zip offline (pobiera ~1 GB, na końcu robi selftest).
4. **`make_release.bat --publish`**:
   - buduje oba zipy w `dist\`,
   - tworzy tag `v0.1.0` i wypycha go,
   - tworzy release na GitHubie i wgrywa zipy (używa `gh` z `github_setup.bat`).
   Bez `--publish` tylko buduje (`dist\`), `--no-offline` pomija duży zip.

Po wypchnięciu tagu GitHub Actions (`release.yml`) dodatkowo uruchamia testy i dokłada source zip –
to normalne, że w zakładce Actions pojawi się workflow `release`.

## Ręcznie przez stronę (gdy `gh` nie działa)

1. `make_release.bat` (bez `--publish`) → zipy w `dist\`.
2. GitHub → repo → **Releases → Draft a new release**.
3. **Choose a tag** → wpisz `v0.1.0` → *Create new tag on publish*. Tytuł: `MultiConnect v0.1.0`.
4. Przeciągnij oba zipy z `dist\` w pole „Attach binaries”. **Publish release**.

Limit GitHuba: 2 GB na plik. Skrypt ostrzeże, jeśli zip offline jest większy – wtedy dajemy go pendrivem.

## Zip na pendrive (bez commita, bez GitHuba)

```bash
make_release.bat --pendrive
```
Buduje `dist\multiconnect-v0.1.0-offline.zip` z **aktualnych** plików (też niezacommitowanych), z tym samym
filtrem `.gitignore` i tą samą kontrolą sekretów. Wymaga wcześniejszego `make_offline_bundle.bat`. Nic nie publikuje.

Na docelowym PC:
1. Skopiuj zip z pendrive'a i rozpakuj do **krótkiej ścieżki**, np. `D:\mc` albo `C:\mc`
   (Windows ma limit 260 znaków ścieżki – głęboko zagnieżdżony folder może się nie rozpakować).
2. `D:\mc\multiconnect\setup.bat` – bez internetu kopiuje `uv` z `offline\`, odtwarza Pythona i paczki z `.tools\`,
   bierze Chromium z `.browsers\`, Godota z `.godot_bin\` / `offline\godot.zip`.
3. `selftest.bat` → `SELFTEST PASSED`.

## Pobieranie na PC zespołu

Repo → **Releases** → najnowsza wersja → pobierz zip → rozpakuj np. do `D:\multiconnect`
(nie do `Program Files` – tam nie ma prawa zapisu bez admina) → `setup.bat` → `selftest.bat`.
Repo jest publiczne, więc pobrać może każdy, bez logowania (https://github.com/panPianka/multiconnect/releases).

## Poprawka w trakcie hackathonu

Zmiana w kodzie → commit → push → na pozostałych PC `git pull` (szybciej niż nowy release).
Release robimy tylko dla „stabilnych” punktów albo gdy ktoś nie ma gita.
