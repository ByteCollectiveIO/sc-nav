---
name: deploy
description: Cut a release for the SC nav project — run tests, bump the SemVer in server/version.py, commit, annotated-tag vX.Y.Z, and push to main. Use when the user says "/deploy", "cut a release", "ship it", or "tag a new version". Stops at the push; the server-side pull + docker rebuild is done manually by the user.
---

# /deploy — cut a release

Encodes this project's release workflow (see the `release-versioning` memory).
The user handles the server-side pull/rebuild themselves, so this skill **ends at
the push**. Do the steps in order; stop and report if any preflight check fails.

## Argument

`$ARGUMENTS` may be:
- a bump keyword: `patch`, `minor`, or `major`
- an explicit version: `0.2.0`
- empty — then look at `git log <last-tag>..HEAD --oneline` and **propose** a bump
  (minor for new features, patch for fixes; still 0.x so breaking changes ride a
  minor bump), and confirm the resulting version with the user before committing.

## Steps

1. **Preflight.**
   - Confirm the branch is `main` (`git branch --show-current`). If not, stop and ask.
   - `git status --short`. If there are uncommitted changes, show them and ask the
     user whether they belong in this release. Only commit what they confirm, with a
     clear `Area: summary` message. **Never `git add -A`** — stage explicit paths so
     untracked files (`.env`, this skill, local configs) are never swept in.

2. **Test gate.** Run `cd server && .venv/bin/python test_nav_core.py`. If it is not
   green, stop and report the failure — do not tag a red build.

3. **Compute the version.** Read the current string from `server/version.py`
   (`__version__`). Apply the bump (or use the explicit version). Reject a version
   that isn't strictly greater than the current one.

4. **Bump.** Edit `server/version.py` so `__version__` is the new version.

5. **Commit the release.** `git add server/version.py`, then:
   ```
   git commit -m "Release v<X.Y.Z>

   <one-line summary of what's in this release, from the log since the last tag>

   Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
   ```

6. **Tag.** Annotated tag with a short changelog from `git log <last-tag>..HEAD`:
   ```
   git tag -a v<X.Y.Z> -m "v<X.Y.Z>

   <bulleted highlights since the previous tag>"
   ```

7. **Push.** `git push origin main --follow-tags` (pushes the commit + the tag).

8. **Report back.** State the pushed version, then remind the user of the manual
   server step (they run this themselves):
   ```
   cd <repo> && git pull --ff-only origin main && docker compose up -d --build sc-nav
   ```
   and that `/api/health` + the footer should then read the new version.

9. **Update memory.** Update the `release-versioning` memory's latest-version note to
   the version just shipped.

## Guardrails
- Abort on: not on `main`, red tests, a non-increasing version, or a dirty tree the
  user hasn't accounted for.
- Do not touch the server, SSH, or docker — that's the user's manual step.
- Stage explicit paths only; never `git add -A` / `git add .`.
