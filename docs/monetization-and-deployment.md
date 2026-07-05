# Monetization & Deployment Strategy

Status: **exploratory / parked** (2026-06-28). No revenue model committed. This
doc captures the strategic lay of the land and — critically — what CIG's fan
content rules actually permit, so we don't have to re-derive it later.

> Not legal advice. Based on CIG's public FAQs as of June 2026; these policies
> get revised. Anything beyond a donation jar warrants a written request to CIG
> and/or a games-IP attorney before building.

---

## TL;DR

- **Ads:** ❌ Drop entirely. Not permitted outside video platforms.
- **Donations / cost-recovery:** ✅ The realistic ceiling. Must stay framed as
  defraying expenses, not profit.
- **Paid managed SaaS / subscriptions:** ⚠️ Directly conflicts with CIG's
  non-commercial clause. Do **not** build unilaterally — requires CIG permission.

---

## The two constraints that frame everything

**1. CIG fan-content rules are the gating factor.** This is a Star Citizen
fansite built on CIG IP (location/ship names, lore, data, Fankit assets).
Commercializing it is restricted (details below). We already show the
trademark/fansite disclaimer on the login splash + launcher — keep that.

**2. The Windows watcher means "host it for the org" is never fully turnkey.**
Live navigation depends on a per-user local agent reading `Game.log`. Every
member who wants live position must install and trust a local watcher. That
onboarding friction (installer, code signing, "why is this reading my game
folder") is a bigger adoption ceiling than where the web app runs, and it
exists in every hosting model. Worth solving regardless of monetization.

---

## What CIG actually permits

Governing line, from the Fankit & Fandom FAQ (broad, and the one that matters):

> Any use of CIG Fankit or other material belonging to CIG is limited to
> **non-commercial use only** and **may not be used to collect, receive, or
> solicit revenue, subscription fees, or compensation of any kind.**

Supporting principles, consistent across their policies:

- **Ads:** the *only* ad carve-out is for video — ad revenue permitted via
  "YouTube Partner Program and similar programs on video-sharing sites." No
  equivalent allowance for ads on a fan website or app.
- **Cost-recovery tolerated, profit not.** Bar Citizen meetups may charge
  "reasonable door charges… solely to defray expenses, but not to turn a net
  profit." Fan films may fundraise up to **$50,000 net, for costs/expenses
  only.** Consistent principle: *recover expenses, don't profit.*
- **Disclaimer required** (already in place).
- Non-commercial "unless we grant otherwise" — CIG **does** grant case-by-case
  arrangements to community projects.

### Scoring the models

| Model | Verdict |
|---|---|
| Ads | ❌ Not permitted outside video platforms. |
| Donations / "defray costs" | ✅ Safest. Must be cost-recovery, not profit. Keep modest + expense-tied. |
| Paid managed SaaS / subscription | ⚠️ Conflicts with the non-commercial clause. Could draw a C&D. |

**Important nuance:** the open-core "sell managed *hosting/ops* as a service,
not the software" idea does **not** cleanly escape the clause. Even framed as
selling our time/ops, we'd be soliciting subscription fees for a product built
on CIG's protectable content. "Compensation of any kind" is broad enough to
cover it.

---

## Deployment models considered

- **Self-hosted Docker (current).** Goodwill engine + CIG-safe (we give the
  software away). Make it trivially deployable: single `docker-compose`, env
  config, one-click templates (Fly.io / Railway). Lowers support burden, grows
  the free base.
- **Managed multi-tenant (single instance, row-level org isolation).** Feasible
  today — org scoping already exists in the schema (see multi-user migration).
  Cheapest to operate. *Gated on CIG permission before charging.*
- **Per-org instances.** Cleaner isolation + easier billing, more ops burden.
  Only justified by isolation/noisy-neighbor reasons; multi-tenant can bill fine.

---

## Options beyond the original three

- **One-click deploy templates** — non-technical org leaders self-host in
  minutes. Grows free base, cuts support cost.
- **Org sponsorship over individual donations** — orgs have treasuries; one flat
  monthly sponsor beats nickel-and-diming members and is less awkward. (Still
  subject to the cost-recovery framing.)

---

## Recommended path (when we pick this back up)

1. **Now / safe:** add a donation link (Ko-fi / "Buy me a coffee"), framed as
   covering hosting + expenses. Zero lock-in, zero IP risk.
2. **Drop ads entirely.**
3. **If we ever want to charge orgs for hosting:** send CIG a written permission
   request *first* — do not build the paid tier on the assumption it's allowed.
   (Draft not yet written; revisit this doc.)
4. **Reduce IP surface** where practical — the more the tool is our original
   software that *happens to work with* SC, and the less it leans on Fankit
   logos/branded assets, the smaller the bite of the non-commercial clause.
   (Never zero — names/data are theirs.)
5. **In parallel, regardless of money:** treat the watcher installer/onboarding
   as a first-class product problem. It's the real adoption ceiling.

---

## Next action (parked)

Draft the CIG permission inquiry so the ask is ready if/when we want a paid
hosting tier. Not started.

## Sources

- [Fan Film and Machinima Policy](https://support.robertsspaceindustries.com/hc/en-us/articles/5422808416151-Fan-Film-and-Machinima-Policy)
- [Star Citizen Fankit and Fandom FAQ](https://support.robertsspaceindustries.com/hc/en-us/articles/360006895793-Star-Citizen-Fankit-and-Fandom-FAQ)
- [Fandom FAQ – Videos, writing, and more](https://support.robertsspaceindustries.com/hc/en-us/articles/115013196127-Fandom-FAQ-Videos-writing-and-more)
- [Video Monetization FAQ](https://robertsspaceindustries.com/faq/video-monetization-faq)
