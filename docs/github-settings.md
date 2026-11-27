# GitHub settings the pipes need

Nothing here is applied by the repository itself. A maintainer applies it once, in this order. The order matters for
the first step: the zips leave git in the same pull request that makes the deploy serve them from releases.

## 1. Before merging the pull request that brings these pipes

- [ ] **Publish the three Maid Lash draft releases** (`maid-lash-1.0.0`, `maid-lash-1.0.1`, `maid-lash-1.0.2`).
      Publishing creates their tags; their assets are the zips the site already serves, byte for byte. Until they
      are published, the `check` job fails on purpose ("release ... is a draft").
- [ ] **Secret `PRIVACY_WALL`** (Settings > Secrets and variables > Actions > New repository secret): the names
      that must never appear here, one per line. Without it `check` fails closed. GitHub masks it in logs, and the
      check never prints a match, only where it is.
- [ ] **Variables** (same page, Variables tab):
  - `MAINTAINER` = `worflor` (whose approval the merge gate wants, and who is asked to review a failed routine PR)
  - `ROUTINE_BOT` = the login of Understudy's bot identity (below), e.g. `understudy-woflo[bot]`. Until it is set,
    no pull request can pass as routine.
- [ ] **Label `routine`**: "Patch-day proof and compatibility update from Understudy; auto-merges when green".
- [ ] **Pages source: GitHub Actions** (Settings > Pages > Build and deployment > Source). Today it is "Deploy from a
      branch" (`main`, `/`). After the switch the current site keeps being served until the first Actions deploy.
      Check right after switching: `curl -sI https://woflo.dev/deadlock-skins/dmm-source.json` answers `200`.
      Then merge; the `store` workflow on `main` checks, builds, deploys and runs `smoke`, which fetches every
      served path and checks every zip's sha256. If `smoke` fails, the job log names the path.
- [ ] Environment `github-pages` (created by the switch): deployment branches "Selected branches", `main` only.

## 2. Repository settings (Settings > General > Pull Requests)

- [ ] Allow squash merging: on. Merge commits and rebase merging: off (one commit per change on `main`).
- [ ] **Allow auto-merge: on.**
- [ ] **Automatically delete head branches: on.**

## 3. Branch protection for `main` (Settings > Rules > Rulesets > New branch ruleset, target `main`)

- [ ] Restrict deletions; block force pushes.
- [ ] Require a pull request before merging. **Required approvals: 0.** Reviews are enforced by the `merge-gate`
      check instead, because a routine pull request must merge with no reviewer, and the maintainer cannot approve
      a pull request opened under their own account.
- [ ] Require status checks to pass: `check`, `routine-guard`, `merge-gate`. "Require branches to be up to date":
      off (nothing generated is committed, so two green pull requests cannot conflict except in git itself).
- [ ] Bypass list: empty, the maintainer included, so a commit on `main` has always passed `check` before it
      deploys. (If the maintainer keeps a bypass for emergencies, the deploy still only runs after `check` passes.)

## 4. Understudy's bot identity

A GitHub App is the cleanest identity: its own login, a token that expires within the hour, and no seat. Create it
under the maintainer's account (Settings > Developer settings > GitHub Apps), install it on this repository only, with:

| Permission | Level | For |
|---|---|---|
| Contents | Read and write | push its branches |
| Pull requests | Read and write | open the routine PR, enable auto-merge |
| Metadata | Read | required |
| Workflows | none | it must never touch `.github/` (and `routine-guard` fails any routine PR that does) |

Its private key lives on the Understudy machine only. A machine user with a fine-grained token limited to this
repository and the same permissions works too.

What each identity may do:

| Who | May | May not |
|---|---|---|
| Understudy's bot | push branches, open PRs, label its own PR `routine`, enable auto-merge on it, request review | push to `main`, merge a PR that is not routine (the gate stops it), change settings, touch releases |
| The maintainer (`worflor`) | everything: review, merge, publish releases, change settings | merge red (the ruleset requires the checks) |
| Workflows (`GITHUB_TOKEN`) | read the repo; deploy Pages from `main`; switch auto-merge off, drop the `routine` label and request review when `routine-guard` fails | push, release |

## 5. After the first deploy

- [ ] `python tools/store.py smoke` from any machine: every served path answers `200` with no redirect.
- [ ] In a DMM with mod sources, refresh the source: no "origins changed" message, Maid Lash still installed, no
      update offered (same release id).
