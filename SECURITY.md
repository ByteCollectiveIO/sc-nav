# Security policy

## Reporting a vulnerability

Please report security issues **privately**, not as a public GitHub issue.

Use [GitHub's private vulnerability reporting][gh-report] on this repository
(Security → Report a vulnerability). If that isn't available to you, open a
public issue containing only "security report, please provide a private
channel" — with no details — and a maintainer will follow up.

[gh-report]: https://github.com/ByteCollectiveIO/sc-nav/security/advisories/new

Useful things to include, as far as you have them: what an attacker gains, the
smallest request or steps that demonstrate it, and which version
(`/api/health` reports it once you're signed in, and it's in the page footer).

This is a volunteer-run hobby project for Star Citizen orgs, not a funded
product. There is no bug bounty, and response times depend on people's evenings.
Expect an acknowledgement within about a week.

## What's in scope

The server, the single-file SPA, the Windows watcher, and the shipped
deployment configuration in this repository.

Out of scope, because they aren't ours to fix: Discord, Cloudflare, and the
upstream data feeds (UEX, the Star Citizen Wiki, starmap.space, Strata/CELD).
Findings that require an org admin to already be malicious are also out of
scope — an admin can set the webhook URLs and the org's settings, and the
threat model treats them as trusted (see below).

## Which versions get fixes

The latest release only. Self-hosting orgs are expected to redeploy; the ORG
ADMIN → SERVER VERSION panel checks the upstream releases feed and says when
you're behind. Releases are published per version, so [watching the repo][watch]
(Custom → Releases) is how you learn a security fix exists.

[watch]: https://github.com/ByteCollectiveIO/sc-nav/subscription

## Trust model, stated plainly

Worth knowing before you decide something is a vulnerability — these are
deliberate design positions, documented in full in
[`docs/security-review-2026-08.md`](docs/security-review-2026-08.md):

- **The org's server operator is fully trusted.** They build the image and serve
  the watcher bundle that members download. There is no code signing, so a
  compromised server can ship malicious code to its own members. Nothing in the
  watcher gives that a runtime foothold afterwards — it never self-updates,
  downloads code, or evaluates anything it didn't ship with.
- **Org admins are trusted** with org data and settings. Privilege escalation
  *to* admin is very much in scope; an admin misusing admin powers is not.
- **Members are semi-trusted.** Some surfaces are deliberately shared-write
  (marking a mining node depleted, confirming a danger warning) because an org
  tool where everyone can contribute is the point. Abuse of a shared-write
  surface is a design trade-off we've accepted; a member reading or writing
  *another member's* private data is a bug — please report it.
- **Watcher tokens are scoped** to the three endpoints the watcher posts to. A
  token reaching anything else is a bug.

## For members running the watcher

If you think your watcher token has leaked, revoke it yourself: Settings →
watcher tokens → delete, then download a fresh bundle from the Setup page. Your
token lives in plaintext in `watcher_config.json`, so keep that folder out of
cloud-synced locations. `watcher/README.md` documents exactly what the watcher
reads and sends.
