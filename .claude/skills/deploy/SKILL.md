---
name: deploy
description: Cut a release for the SC nav project via a gated PR — run tests, bump the SemVer in server/version.py, and open a release PR. Use when the user says "/deploy", "cut a release", "ship it", or "tag a new version". Once the user merges the PR, the `tag-release` GitHub Actions workflow tags it automatically; this skill never merges the PR and never touches the server.
---

# /deploy — cut a release (PR-gated, auto-tagged)

Encodes this project's release workflow (see the `release-versioning` memory).
This skill opens a release PR and **stops**. The user merges it manually — a
deliberate review gate, kept so a future second developer still gets a real
review. Once the PR is merged, the **`tag-release`** GitHub Actions workflow
(`.github/workflows/tag-release.yml`) reads `server/version.py` and pushes the
matching `vX.Y.Z` tag automatically, after the `tests` check passes on `main`. So
there is **no manual tagging step** — the old two-pass flow is gone. This skill
never merges the PR and never touches the server; both stay manual.

## Argument

`$ARGUMENTS` may be:
- a bump keyword: `patch`, `minor`, or `major`
- an explicit version: `0.2.0`
- empty — then look at `git log <last-tag>..HEAD --oneline` and **propose** a bump
  (minor for new features, patch for fixes; still 0.x so breaking changes ride a
  minor bump), and confirm the resulting version with the user before committing.

## Steps

1. **Preflight.**
   - Confirm the current branch is `main` (`git branch --show-current`). If not,
     stop and ask.
   - `git fetch origin main`, then confirm local `main` is up to date with
     `origin/main` (`git rev-parse main` == `git rev-parse origin/main`). If
     behind, pull first; if diverged, stop and ask.
   - `git status --short`. If there are uncommitted changes, show them and ask the
     user whether they belong in this release. Only commit what they confirm, with
     a clear `Area: summary` message. **Stage explicit paths and commit an explicit
     path list** (`git add <path>…` then `git commit <path>… -m …`). Never
     `git add -A` / `git add .`, and never a bare `git commit -m` after staging —
     either can sweep in unrelated staged files (`.env`, local skill/tooling edits)
     that shouldn't be in the release.

2. **Test gate.** Run both, matching CI exactly so a red build can't open a PR:
   ```
   cd server && .venv/bin/python test_nav_core.py
   cd server && .venv/bin/python test_app.py
   ```
   If either is not green, stop and report the failure.

3. **Compute the version.** Read `__version__` from `server/version.py`. Apply the
   bump (or use the explicit version). Reject a version that isn't strictly greater
   than the current one.

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
   - Merge it themselves (GitHub UI or `gh pr merge`) — this skill will not.
   - On merge, the `tag-release` workflow auto-creates and pushes `v<X.Y.Z>` once
     `tests` passes on `main` — no further `/deploy` run needed. Confirm via
     `git fetch --tags` or the Actions tab.
   - Redeploy is **automatic** — no manual step. Prod runs as a **git-based
     Portainer stack** on the Proxmox/Ubuntu VM with **5-minute polling** enabled:
     Portainer re-pulls `main` and redeploys (it owns the checkout and the
     `build: .`) within ~5 min of the merge. The old `git pull && docker compose
     up --build` SSH flow is gone. Tell the user to just wait a poll cycle, then
     confirm `/api/health` + the footer read the new version. (If they ever want
     it instant, they can hit **Pull and redeploy** on the `sc-nav` stack in the
     Portainer UI.)

10. **Update memory.** Update the `release-versioning` memory's latest-version note
    to the version just shipped.

## Guardrails
- Never push directly to `main` — this skill only ever pushes a `release/*` branch.
- Never merge the PR — that's the user's manual call, on purpose.
- Tagging is automatic (the `tag-release` workflow). Do not create tags by hand
  unless that workflow failed and the user asks you to backfill one.
- Abort on: not starting from an up-to-date `main`, red tests, a non-increasing
  version, or a dirty tree the user hasn't accounted for.
- Stage explicit paths only; commit an explicit path list — never `git add -A` and
  never a bare `git commit -m` that could include unrelated staged changes.
- Do not touch the server, SSH, docker, or Portainer. Redeploy is fully automatic
  via Portainer's 5-minute git-stack polling — there is no manual redeploy step to
  perform or to instruct the user to perform (beyond merging the PR).

## How the deployed app updates (Portainer git-stack polling)
Prod is a git-based Portainer stack polling `main` every **5 minutes**. When a
release PR merges, the sequence is fully hands-off after the merge:
merge → `tests` passes on `main` → `tag-release` pushes `vX.Y.Z` → within ~5 min
Portainer re-pulls the new SHA and rebuilds/redeploys the `sc-nav` stack. So the
whole `/deploy` job ends at "PR is merged"; the running app catches up on its own.
For an instant redeploy the user can click **Pull and redeploy** in Portainer, but
that's optional. (Alternatives not in use: a redeploy webhook chained off CI would
be instant but needs a Cloudflare tunnel route + Access token to reach Portainer —
more setup than the polling is worth.)
