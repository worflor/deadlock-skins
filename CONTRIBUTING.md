# How the store works

This repository is woflo's Deadlock skin store: the pages at https://woflo.dev/deadlock-skins/ and the mod source
Deadlock Mod Manager (DMM) players add. It holds finished, released mods only. Every pipe is below; nothing is done
by hand twice.

## The pipes

```
skins/<id>/manifest.json + page.html          one folder per mod: the only list of mods
GitHub release <id>-<version>                 the release zip, one asset (the repository holds no zips)
        |
        v
tools/store.py check   (CI on every PR and on main; Understudy before it pushes)
  schema, names, release assets and their sha256, the zips' contents, pak slots and paths, the Valve check record,
  proofs and every compatibility claim, the privacy wall, then the site and catalog built with the pinned Magician
        |
        v  main only, and only when check passed
deploy: the tree check built goes to GitHub Pages, zips included, at the same URLs as always
        |
        v
smoke: every served path answers, every zip is the manifest's bytes
```

- **The manifest** is the contract: [docs/manifest.md](docs/manifest.md). Its schema is
  [schema/mod.schema.json](schema/mod.schema.json).
- **The generator** is Magician's `source` tool, from the release pinned in [magician.lock.json](magician.lock.json)
  (version and sha256 of each platform's archive). Never `main`, never "latest". Changing the pin is a reviewed pull
  request.
- **The store's look** is `theme/`, its name and address `store.json`, DMM's taxonomy `taxonomy.json`.
- **GitHub settings** the pipes rely on: [docs/github-settings.md](docs/github-settings.md).

## Kinds of pull request

| Kind | Who opens it | What it may touch | How it merges |
|---|---|---|---|
| **Routine** (label `routine`) | Understudy's bot only | proof rows (add only) and each release's `proven_on`, `pending_on`, `valve_check` in existing manifests | auto-merge (squash, branch deleted) when `check`, `routine-guard` and `merge-gate` pass; no reviewer |
| **Normal** | anyone with access | anything: a new release, a new mod, copy, CI, tooling | the maintainer reviews and merges |

`routine-guard` runs the base branch's copy of `tools/store.py` against the pull request, so a pull request cannot
loosen the rule it is judged by. If a routine pull request touches anything else, the guard fails, switches
auto-merge off, removes the label and asks the maintainer for review.

`merge-gate` passes a routine pull request from the bot, passes the maintainer's own pull request (merging it is the
review), and otherwise waits for the maintainer's approval of the newest commit.

## Adding a release

1. Build the zip with Magician: `magician bundle <bundle.json> --zip` (it refuses files identical to Valve's).
2. Where a Magician baseline of the current build is (`magician baseline`), record the release from the zip:

   ```
   python tools/store.py record --mod <id> --zip <the zip> --baseline <reference>/<build>/baseline.tsv --write
   python tools/store.py proofs --mod <id> --ledger <source repo>/<mod>/proofs.tsv --write
   ```

   `record` fills in every field of the release from the zip itself, and the Valve check from the baseline.
3. Create the GitHub release `<id>-<version>` with the zip as its only asset. Draft is fine while the pull request is
   reviewed; `check` passes only once it is published, so nothing is offered before its bytes are downloadable.
4. Open a normal pull request with the manifest change. Publish the release, let `check` go green, merge.

A new mod is the same with a new `skins/<id>/` folder: copy a manifest, set the listing, put the page copy in
`page.html`, and `record` its first release.

## Running the checks

```
python -m unittest discover -s tools -p "test_*.py"
python tools/store.py check --privacy-file <file of withheld names> --site <out dir>
```

`check` downloads the release zips into `.cache/` (once; each is verified by sha256) and the pinned Magician, and
needs Pillow (`pip install --require-hashes -r tools/requirements.txt`). `--offline --assets <dir>` checks zips from a
folder instead of GitHub; `--allow-draft` accepts a draft release when your token can see it (CI never passes it).

## The privacy wall

Only released mods ever appear here: in files, zips, the built site, pull request titles and bodies, branch names
and commit messages. `check` fails on any name of unreleased work. The names are not in this repository; CI reads them
from the `PRIVACY_WALL` secret, a maintainer's machine from a file (`--privacy-file`). The check says where a name is,
never which name.

## Commits

Plain messages saying what changed, one change per pull request, squash-merged.
