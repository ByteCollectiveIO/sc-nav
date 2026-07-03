---
name: deploy
description: Cut a release for the SC nav project via a gated PR — run tests, bump the SemVer in server/version.py, open a release PR, then (after the user reviews CI and merges it manually) tag the merged commit and push the tag. Use when the user says "/deploy", "cut a release", "ship it", or "tag a new version". Never merges the PR and never touches the server — both stay manual.
---

# /deploy — cut a release (PR-gated)

Encodes this project's release workflow (see the `release-versioning` memory).
This skill runs in **two passes** so a release can never reach `main` unless the
GitHub Actions check (`tests.yml`) has run on it first. It never merges the PR —
that's a deliberate manual step, kept so a future second developer still gets a
real review gate — and it never touches the server, which the user always does
themselves.

## Argument

`$ARGUMENTS` may be:
- a bump keyword: `patch`, `minor`, or `major`
- an explicit version: `0.2.0`
- empty — then look at `git log <last-tag>..HEAD --oneline` and **propose** a bump
  (minor for new features, patch for fixes; still 0.x so breaking changes ride a
  minor bump), and confirm the resulting version with the user before committing.

Only relevant to Pass 1 (opening a new release). Pass 2 takes no argument — it's
just "finish the release that's already merged."

## Which pass am I in?

Before doing anything, run `git fetch origin main` then check the HEAD commit of
`origin/main`:

- If its subject line matches `^Release v(\d+\.\d+\.\d+)$` **and** the tag
  `v<that version>` does **not** already exist (`git rev-parse -q --verify
  refs/tags/v<version>` fails) → **Pass 2** (tag the already-merged release).
- Otherwise → **Pass 1** (cut a new release).

If it's ambiguous (e.g. the user passed an explicit version but a different
release looks mid-flight), stop and ask.

## Pass 1 — open the release PR

1. **Preflight.**
   - Confirm the current branch is `main` (`git branch --show-current`). If not,
     stop and ask.
   - Confirm local `main` is up to date with `origin/main` (`git rev-parse main`
     == `git rev-parse origin/main`, after the fetch above). If behind, pull
     first; if diverged, stop and ask.
   - `git status --short`. If there are uncommitted changes, show them and ask
     the user whether they belong in this release. Only commit what they
     confirm, with a clear `Area: summary` message. **Never `git add -A`** —
     stage explicit paths so untracked files (`.env`, local configs) are never
     swept in.

2. **Test gate.** Run both, matching CI exactly so a red build can't get as far
   as opening a PR:
   ```
   cd server && .venv/bin/python test_nav_core.py
   cd server && .venv/bin/python test_app.py
   ```
   If either is not green, stop and report the failure.

3. **Compute the version.** Read the current string from `server/version.py`
   (`__version__`). Apply the bump (or use the explicit version). Reject a
   version that isn't strictly greater than the current one.

4. **Branch.** Create `release/v<X.Y.Z>` off `main`.

5. **Bump.** Edit `server/version.py` so `__version__` is the new version.

6. **Commit.** `git add server/version.py`, then:
   ```
   git commit -m "Release v<X.Y.Z>

   <one-line summary of what's in this release, from the log since the last tag>

   Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
   ```

7. **Push the branch.** `git push -u origin release/v<X.Y.Z>`.

8. **Open the PR.**
   ```
   gh pr create --base main --head release/v<X.Y.Z> \
     --title "Release v<X.Y.Z>" \
     --body "<bulleted highlights since the previous tag>"
   ```

9. **Report back.** Give the PR URL and tell the user explicitly:
   - Wait for the `tests` check to go green on the PR.
   - Merge it themselves (in the GitHub UI or `gh pr merge`) — this skill will
     not do it.
   - Once merged, run `/deploy` again (no argument needed) to tag the merged
     commit and push the tag.

Stop here. Do not tag. Do not touch `main` directly.

## Pass 2 — tag the merged release

1. **Sync.** `git checkout main && git merge --ff-only origin/main` (main should
   fast-forward cleanly onto the merged PR commit). If it doesn't fast-forward,
   stop and ask — that means `main` has unexpected local commits.

2. **Verify.** Confirm `HEAD`'s subject is `Release v<X.Y.Z>` and that
   `server/version.py` actually reads `<X.Y.Z>`. As a sanity check, confirm
   GitHub Actions succeeded on this commit: `gh run list --commit "$(git
   rev-parse HEAD)" --workflow tests.yml --limit 1`. If it didn't succeed (or no
   run is found), stop and tell the user — don't tag an unverified commit.

3. **Tag.** Annotated tag with a short changelog from `git log <last-tag>..HEAD`:
   ```
   git tag -a v<X.Y.Z> -m "v<X.Y.Z>

   <bulleted highlights since the previous tag>"
   ```

4. **Push the tag.** `git push origin v<X.Y.Z>`. (`main` itself is already up to
   date on `origin` from the merge — don't push `main`.)

5. **Clean up the release branch**, if it still exists:
   `git push origin --delete release/v<X.Y.Z>` and `git branch -d
   release/v<X.Y.Z>` (skip silently if GitHub already auto-deleted it on merge).

6. **Report back.** State the tagged version, then remind the user of the
   manual server step (they run this themselves):
   ```
   cd <repo> && git pull --ff-only origin main && docker compose up -d --build sc-nav
   ```
   and that `/api/health` + the footer should then read the new version.

7. **Update memory.** Update the `release-versioning` memory's latest-version
   note to the version just shipped.

## Guardrails
- Never push directly to `main`. Pass 1 only ever pushes a `release/*` branch;
  Pass 2 only ever fast-forward-pulls an already-merged `main` to tag it.
- **Never merge the PR** — that's the user's manual call, on purpose.
- Abort on: not starting from an up-to-date `main`, red tests, a non-increasing
  version, a dirty tree the user hasn't accounted for, or (Pass 2) a merged
  commit that doesn't match the expected release version/message, or a missing/
  failed CI run on that commit.
- Do not touch the server, SSH, or docker — that's the user's manual step.
- Stage explicit paths only; never `git add -A` / `git add .`.
