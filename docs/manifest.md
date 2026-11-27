# The mod manifest: `deadlock-skins.mod/1`

This is the contract between the store and everything that reads or writes it: people, CI, and Understudy (the
patch-day service that re-proves the mods on each new Deadlock build). The schema is
[`schema/mod.schema.json`](../schema/mod.schema.json); `tools/store.py check` enforces it and everything below.

## A mod is a folder

```
skins/<id>/manifest.json   the manifest
skins/<id>/page.html       the store page copy (simple HTML: headings, paragraphs, lists, quotes, rules)
```

Adding a folder adds a mod. Nothing else in the repository lists the mods: the store pages, the Deadlock Mod Manager
(DMM) catalog, the feed and the sitemap are all generated from the manifests on every deploy, and none of them is
in git. A reader that wants to know what the store offers scans `skins/*/manifest.json`.

The manifest says everything about a mod that is public: its listing, every release with its download and
checksums, the result of the Valve check, and the public part of its proof ledger. Its sources live elsewhere, in the
private repository `source.repo`.

## Versioning

`schema` is `deadlock-skins.mod/1`. Unknown fields are refused, so the schema and the files can never drift apart. A
change that a `/1` reader can ignore safely (a new optional field) is still `/1` and lands with the schema change in
the same pull request. A change that alters the meaning of an existing field is `/2`. A reader refuses a schema id it
does not know rather than guessing.

## The fields

| Field | What | Who changes it |
|---|---|---|
| `schema` | `"deadlock-skins.mod/1"` | a schema change |
| `id` | The listing id, the folder's name and DMM's id for the mod. Permanent. | never |
| `status` | `"released"`, or `"early"`: the store and DMM say "Early release" with what it is proven on and what is pending | maintainer |
| `rights` | `"cleared"` (the author reviewed what ships and publishes it) or `"unreviewed"` (a warning, not a block) | maintainer |
| `source.repo`, `source.mod_toml` | The private source repository (`owner/name`) and the path of the mod's `mod.toml` in it; its `proofs.tsv` sits beside it | maintainer |
| `listing.*` | What the store and DMM show: `name`, `author`, `hero` (display), `hero_id` and `content_type` (DMM's taxonomy ids, checked against `taxonomy.json`), `nsfw`, `description` (one line), `tags`, `link`, `tagline`, `blurb` (the card), `links` (`kind` `"same"` for the mod on another site, `"video"` for a trailer), `page` (the copy file), `sections` (README sections of the zip to append), `preselect` (optional components ticked by default) | maintainer |
| `releases[]` | Oldest first; the last one is what is offered | maintainer, except the three fields below |
| `releases[].proven_on` | The builds a complete proof holds every pak of this release on. Must equal what `proofs` says. | **routine** |
| `releases[].pending_on` | Builds a re-prove is waiting on. Never also in `proven_on`. | **routine** |
| `releases[].valve_check` | The Valve check, re-run against a newer build's baseline | **routine** |
| `proofs[]` | Public proof rows. Rows are only ever added. | **routine** (add only) |

A pull request labelled `routine` may change only the fields marked routine, in manifests that already exist, and
only Understudy's bot may open one. Everything else is a reviewed change.

### A release

| Field | What | Checked against |
|---|---|---|
| `version` | Public semver, e.g. `1.0.2` | the zip's `mod-interchange.json` |
| `date` | `YYYY-MM-DD` | the zip |
| `build` | Magician's internal build label, e.g. `b45` | the zip |
| `built_for` | The Deadlock build it was made against. Not a proof. | the zip |
| `components[]` | One per pak, in load order: `id`, `name`, `description`, `order`, `optional`, `pak` (its path in the zip, `files/NNNN-<id>/pakNN_dir.vpk`), `slot` (the NN), `sha256`, `size` | the zip, byte for byte |
| `scenes` | Heroes whose hero-page scene patch the zip carries | the zip |
| `previews` | Picture files in the zip's `previews/`, in the order the store shows them | the zip |
| `asset` | The release zip as a GitHub release asset: `tag` (`<id>-<version>`), `name`, `url`, `size`, `sha256` | GitHub's record of the asset, and the downloaded bytes |
| `served` | Where players download it: `https://woflo.dev/deadlock-skins/files/<name>` | the built site |
| `valve_check` | `baseline_build`, `files`, `listing_sha256`, `identical` (see below) | the paks in the zip |
| `proven_on`, `pending_on` | See above | `proofs` |

### A proof row

`build`, `date`, `proof` (Magician's proof id), `owner`, `file`, `crc32`, `sha256`, `evidence_sha256`: a row of the
source repository's `proofs.tsv` (`magician proofs/1`) without its evidence path, which names a folder on a private
machine. A proof's id is a hash of its build and every (owner, sha256) pair in it, so a proof that lost or changed a
row no longer adds up and counts for nothing. The manifest holds every row of every proof that contains one of its
releases' paks, including rows for files that do not ship (a hero-page scene built on the player's machine).

The source ledger stays the record; the manifest's rows are its public copy, there so a public CI can check every
claim without reading a private repository. `store.py proofs --mod <id> --ledger <proofs.tsv> --write` merges new
rows in and restates `proven_on`.

## Downloads

Each release's zip is the single asset of the GitHub release `<id>-<version>` in this repository. The repository
itself holds no zips. The deploy downloads each asset, checks its sha256, and serves it from the Pages site at
`served`, beside the catalog, so DMM downloads from the one origin the source declares (`https://woflo.dev`) and
never follows a redirect to GitHub's storage host. That keeps the source's identity fixed: a DMM player's source
breaks ("Source identity or network origins changed") if its declared origins ever change. Every zip of every
release stays served, so a download already linked somewhere keeps working.

## What `tools/store.py check` checks

Everything here needs no game files:

- each manifest parses and matches the schema, its folder is its id, and its names agree (tag, URLs, pak paths);
- each release's asset exists on a published GitHub release, with GitHub's size and sha256 equal to the manifest's,
  and the downloaded bytes too; every pak, preview, scene, version and build field equals what the zip says;
- no two mods claim the same pak slot, and no two ship the same file path (scene targets included);
- the Valve check (below) was recorded for exactly these paks and found nothing;
- every proof adds up to its id, every `proven_on` equals what the proofs say, and every "proven", "tested",
  "works" or "verified" on a build in the tagline, blurb, the description the store shows, or a release's notes
  names a build a complete proof holds those bytes on ("built for build N" is not such a claim);
- no withheld name appears in the repository, the release zips, the built site, or the pull request's title, body,
  branch and commit messages;
- the site and catalog build from the manifests with the pinned Magician, every release's zip is in it, and nothing
  the live store serves today would disappear.

## The Valve check, honestly

A mod must not ship a file byte-identical to Valve's at the same path. Comparing needs Valve's files, and CI has
none. So the comparison runs where a baseline of the game is: `store.py record` (or `check --baseline FILE`) reads
Magician's baseline of a build (`magician baseline`: the path, crc32 and size of every file Valve ships) and compares
every file in the release's paks against it. The result is recorded as `valve_check`: the baseline's build, how many
files were compared, a sha256 of the sorted list of (component, path, crc32, size) that was compared, and how many
matched. CI cannot redo the comparison; it recomputes the listing from the zip and refuses the release if the
recorded check was made for other bytes, or if it found anything. A newer build can make an old file identical to
Valve's new one, so the check is worth re-running on patch day; that update is routine.

Magician's bundler runs a crc check of the paks and the scene patch against the installed game when it builds a zip
(`bundle.py`, unless `--no-valve-check`), and `magician grade` blocks a release on it; the scene patch's files live
inside a map that baselines do not list, so they are covered by the bundler's check, not by `valve_check`.

## For Understudy

- **Discovering mods:** scan `skins/*/manifest.json` at a pinned commit of `main` (or read the DMM catalog, which
  is generated from them and says the same). `source.repo` and `source.mod_toml` name the source; it defaults to
  `<owner>/deadlock-<id>`, which is what both current mods use.
- **Verifying release bytes:** `releases[-1].asset` (GitHub) and `served` (the store) are the same bytes,
  `components[].sha256` the paks inside.
- **After an accepted proof:** stage the ledger patch in the source repository as now, then in a fresh checkout of
  this repository: `python3 tools/store.py proofs --mod <id> --ledger <the updated proofs.tsv> --write` (it also takes
  the build out of `pending_on`), run `python3 tools/store.py check --site <dir>` (the DMM checker
  can then run on `<dir>`), commit on a new branch, push, open a pull request labelled `routine` with no reviewers,
  and enable auto-merge (squash, delete the branch). It merges when the checks pass; the deploy follows.
- **Starting a patch day:** add the new build to each affected release's `pending_on` in the same kind of routine
  pull request, so the store says the check is pending.
- **Anything else** (a rebuilt pak, a new release, copy, a new mod): a normal pull request that requests the
  maintainer's review. The merge gate keeps it from merging without their approval.

## Example

Trimmed from `skins/maid-lash/manifest.json` (one release shown, hashes shortened here only):

```json
{
  "schema": "deadlock-skins.mod/1",
  "id": "maid-lash",
  "status": "released",
  "rights": "cleared",
  "source": {"repo": "worflor/deadlock-maid-lash", "mod_toml": "model-replacements/maid-lash/mod.toml"},
  "listing": {
    "name": "Maid Lash: N°7, Grand Service", "author": "woflo", "hero": "Lash", "hero_id": "lash",
    "content_type": "skins", "nsfw": false, "description": "Lash in a maid's outfit, with matching kit. Cosmetic only.",
    "tags": ["lash", "skin", "maid"], "link": "https://woflo.dev/deadlock-skins/",
    "tagline": "Dinner is served.", "blurb": "Lash, dressed for service: ...",
    "links": [{"label": "Trailer", "url": "https://youtu.be/N_zcC4oYzOI", "kind": "video"}],
    "page": "page.html", "sections": [], "preselect": []
  },
  "releases": [
    {
      "version": "1.0.2", "date": "2026-11-25", "build": "b45", "built_for": 6868,
      "components": [{"id": "maid-lash", "name": "Maid Lash: N°7, Grand Service",
                      "description": "Lash skin: outfit, cloth, VFX, HUD art, sounds and animations. Cosmetic only.",
                      "order": 0, "optional": false, "pak": "files/0000-maid-lash/pak10_dir.vpk", "slot": 10,
                      "sha256": "c19e2aca...", "size": 111233608}],
      "scenes": ["lash"],
      "previews": ["cover.jpg", "lash-front.png", "lash-back.png", "lash-side.png", "lash-apron.png"],
      "asset": {"tag": "maid-lash-1.0.2", "name": "MaidLash_N7-Grand-Service_1.0.2.zip",
                "url": "https://github.com/worflor/deadlock-skins/releases/download/maid-lash-1.0.2/MaidLash_N7-Grand-Service_1.0.2.zip",
                "size": 46193649, "sha256": "01c46154..."},
      "served": "https://woflo.dev/deadlock-skins/files/MaidLash_N7-Grand-Service_1.0.2.zip",
      "valve_check": {"baseline_build": 6871, "files": 644, "listing_sha256": "95b50142...", "identical": 0},
      "proven_on": [6868],
      "pending_on": []
    }
  ],
  "proofs": [
    {"build": 6868, "date": "2026-11-25", "proof": "d55bacaa18141589", "owner": "lash_showcase",
     "file": "maps/ui/hero_prefabs/lash.vpk", "crc32": "d287ca64", "sha256": "81f9345a...", "evidence_sha256": "..."},
    {"build": 6868, "date": "2026-11-25", "proof": "d55bacaa18141589", "owner": "maid_lash", "file": "pak10_dir.vpk",
     "crc32": "edfd3beb", "sha256": "c19e2aca...", "evidence_sha256": "..."}
  ]
}
```
