# Upstream report: SC Wiki API Pyro positions rotated −85.23°

**Status:** drafted 2026-10-04, **not yet filed** with the Star Citizen Wiki
maintainers. Check their API docs for the right tracker before posting.

**Context:** our side of this is fixed. `tools/sync_locations.py` fits and
applies the rotation on every sync, `_meta.frame_aligned` records it, and
`WikiFrameAlignmentTests` pin it (PR #224). The report below is written to be
pasted as-is. It names no member, org or server, and every coordinate in it is
game data.

---

**Title:** Pyro `x`/`y` positions in `/api/locations/positions` are rotated −85.23° about the star compared with in-game coordinates

**Summary**

For `filter[system]=pyro`, the `x`/`y` of every system-parented location
(planets, Lagrange points, asteroid clusters, stations) is rotated by a
constant **−85.23° about the star (z axis)** compared with the positions the
game uses. Distances from the star and `z` are correct. Stanton and Nyx are not
affected (0.00°). Checked against game version **4.10.1-LIVE.12660092**.

**Evidence**

1. **Comparison against an independent in-game-frame dataset** (a community
   starmap containers export). Every Pyro body and Lagrange point matches on
   radius, and the angle differs by exactly −85.230°: spread ≈ 7×10⁻⁷° across
   37 objects from 7 Gm to 71 Gm out.

   | Wiki name | Wiki r (Gm) | Wiki angle | Starmap r (Gm) | Starmap angle | Δ |
   |---|---|---|---|---|---|
   | Terminus | 68.322 | −140.00° | 68.322 | 134.77° | −85.230° |
   | Bloom | 17.802 | −120.00° | 17.802 | 154.77° | −85.230° |
   | Monox | 10.621 | 30.00° | 10.621 | −55.23° | −85.230° |
   | Fuego | 42.994 | −9.81° | 42.994 | −95.04° | −85.230° |
   | PYR6 L3 | 68.322 | 45.00° | 68.322 | −40.23° | −85.230° |
   | PYR1 L4 | 8.273 | −18.00° | 8.273 | −103.23° | −85.230° |

2. **In-game `/showlocation` ground truth at Cluster MNK-833**
   (uuid `f0f2f24f-65af-466c-9f81-eef7b085322f`):
   - API position: (−38,625,123,903.9, −38,306,249,076.8, 608,395,020) m, at
     an angle of −135.2°.
   - A ship inside the field reported (−41,385,512,835, 35,305,910,636,
     608,398,101) m, at an angle of +139.5°. That is the same radius
     (54.40 Gm) and the same z.
   - Rotating the API position by −85.23° puts it 20.5 km from the in-game
     fix, i.e. inside the field. The rotation that maps it exactly onto the
     fix differs from the fitted value by 0.00002°.

3. **Body-relative positions are unaffected.** Pyro outposts expressed
   relative to their planet or moon match the in-game frame within 5 km. Only
   absolute, system-frame `x`/`y` are rotated.

**Likely cause**

The wiki's Pyro angles are round numbers (−140°, −120°, 30°, 45°), while the
in-game angles are offset from them by a fixed −85.23°. This looks like the
Pyro system's root object carrying a rotation in the game data that isn't
applied when absolute positions are computed. Stanton's and Nyx's roots would
have none, which is why they're unaffected.

**Impact**

Any consumer that places free-floating Pyro locations by `x`/`y` (QT asteroid
clusters, stations, Lagrange points, asteroid fields) puts them about 85°
around the star from where they are in game. For the outer clusters that is
tens of Gm.

**Workaround we're using**

Rotate Pyro's system-frame `x`/`y` by −85.23° about z, with θ = −85.2299335°:

- x′ = x·cos θ − y·sin θ
- y′ = x·sin θ + y·cos θ

Use the angle at full precision: an error of 0.0004° is about 380 km at
54 Gm.
