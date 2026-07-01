# SimulateGP — Multi-Game / Public Platform Roadmap

**Status:** Draft for discussion (not started)
**Goal:** Evolve SimulateGP from a single-game classroom tool into a unified app
that can run **many independent simulations at once**, with an eventual path to
**public, self-service** use (strangers create and run their own games).

### Decisions locked in
- **Usernames stay globally unique.** Each login belongs to exactly one game, so
  a team logs in with just username + password and their `game_id` resolves the
  game — no join code, no constraint migration, no login UX change. (Trade-off:
  team logins must be named per class, e.g. `s25a-team1`, since "team1" can only
  exist in one game platform-wide.)

This document is a plan to react to, not a commitment. It is deliberately phased
so each stage is independently useful and testable.

---

## 1. Where we are today

SimulateGP is built around **exactly one game**. The data, though, is already
partly multi-game ready — the refactor is more about the "control plane" (who is
managing which game) than about re-plumbing the simulation itself.

**Single-game assumptions (the blockers):**
- `Game.query.first()` is used in ~15 places in `app.py` — literally "the one
  game." Admin screens and the crank all assume it.
- One hardcoded admin account (`admin` / `admin123`).
- `init_db()` creates a single default game.
- `Team.username` is **globally unique** — two games can't both have a "team1".

**Already in our favor:**
- `Team` and `GameCompany` are tagged with `game_id`. Most team-facing queries
  already filter by game.
- The engine (`game_logic.py`) already runs against a *specific* `game` object
  passed in — it is not hard-wired to a single game.
- Config/lookup tables are separable (see §6).

**Not present today (matters for public):**
- No CSRF protection, no rate limiting, no abuse controls.
- Team passwords are stored in **plaintext** (`Team.password_plain`) so the admin
  can hand out logins. Fine for a classroom; unacceptable for public accounts.

---

## 2. Target architecture (end state)

- **Organizer** accounts (self-registered) — a person who runs one or more games.
- Each **Game** is *owned* by an organizer and has a unique **join code**.
- **Teams** are scoped to a game; a team's username only needs to be unique
  *within its game*.
- Every request resolves a **current game context**:
  - Team users → their `game_id`.
  - Organizers → the game they've selected to manage.
- Strict data isolation: a user can only ever read/write data for their own game.
- Config (return assumptions, tunable dynamics, company library) is either a
  shared default catalog or per-game overridable (decision in §6).

---

## 3. Phase 1 — Multi-game core  *(foundational; unlocks running multiple sections now)*

Goal: a single super-admin (you) can **create, select, and run many games** from
one app. No public accounts yet.

**Data model** (all additive — reuses the existing `_ensure_schema()` pattern)
- Add `Game.name` is already present; add `Game.status`/archive flag if not
  already sufficient for hiding finished games from the switcher.
- Add `Game.owner_id` (nullable now; wired up in Phase 2).
- **No change to `Team.username`** — stays globally unique (decision above). No
  constraint migration, no login change.

**Control plane**
- Introduce a `current_game()` helper:
  - Team user → `current_user.game_id`.
  - Admin → the game selected in the session (falls back to a picker if none).
- Replace every `Game.query.first()` (~15 spots) with `current_game()`.
- Admin **game switcher** (dropdown / landing page) + **Create game** and
  **Archive game** actions. Creating a game seeds its companies from the library.
- Team creation (admin Teams page) assigns the new team to the
  currently-selected game.
- Scope `init_db()` so it no longer assumes/creates a single default game
  (existing prod game becomes Game #1; see migration note).

**Audit**
- Sweep all team-facing routes to confirm every query filters by the current
  game (most already do via `game_id`; a few global lookups need scoping).
- Decide `GameSetting` scoping (see §6) — recommended per-game so one class's
  tuned dynamics don't leak into another.

**Migration of existing data**
- Existing production game keeps `id=1`, stays owned by the super-admin; all
  current teams/companies already point at it via `game_id`. Nothing breaks.

**Testable outcome:** create two games, add teams to each, run them in parallel,
confirm zero cross-contamination (search, deal flow, crank, leaderboard).

---

## 4. Phase 2 — Accounts & self-service  *(makes it a platform)*

- **Organizer signup/login** (email + hashed password; email verification +
  password reset).
- Games owned by organizers; an organizer dashboard lists *their* games only.
- **Join flow for teams:** organizer generates a game with a join code; teams
  self-register into that game (username unique per game) or the organizer
  pre-creates them.
- **Retire plaintext passwords** for any non-classroom use. Keep a clearly-labeled
  "classroom mode" if you still want visible team logins, but default to hashed.
- Roles: super-admin (you) vs organizer vs team.

---

## 5. Phase 3 — Public hardening  *(makes it safe to open up)*

- **Security review + isolation audit** — the single most important item. Verify
  no route can leak or mutate another game's data (ideally enforced centrally,
  not per-route).
- **CSRF protection** on all state-changing forms (Flask-WTF or equivalent).
- **Rate limiting / abuse prevention** (login, signup, search, crank triggers).
- **Session & secrets hygiene** — enforce a real `SECRET_KEY`, secure cookies.
- **Scaling** — gunicorn worker sizing, Postgres connection pooling, and load
  behavior of the crank (it does a lot of work synchronously).
- **Ops** — automated Postgres backups, error monitoring, and
  privacy policy / terms if collecting others' accounts.

---

## 6. Config/lookup tables — decision needed

These are currently **global** (no `game_id`). For multi-game we must decide
shared vs per-game:

| Table | Today | Options |
|---|---|---|
| `CompanyTemplate` | Global library | Keep as a shared catalog; each game seeds its `GameCompany` rows from it. (Recommended: shared.) |
| `ReturnAssumption` | Global (unique per sector/stage) | Shared defaults, with optional per-game override later. |
| `GameSetting` | Global tunable constants | **Should become per-game** so one game's dynamics don't affect another. Needs `game_id` + scoping in `game_settings.apply_overrides()`. |
| `companies.json` seed | One file | Fine as the shared seed catalog for new games. |

---

## 7. Cross-cutting risks

- **Data isolation is security-critical** once public. A missed `game_id` filter
  = one class seeing another's data. Best mitigated by centralizing game-scoping
  rather than trusting every route.
- **The crank is synchronous and heavy.** With many concurrent games this may
  need to move to a background job. Fine to defer, but keep it in mind.
- **Plaintext passwords** must not reach public users.
- **Scope creep** — Phases 2–3 are a genuine product build. Phase 1 alone
  satisfies "unified app running multiple simulations," so it's the safe first
  investment; commit to 2–3 only when the public goal is firm.

---

## 8. Suggested sequencing

1. **Phase 1** — multi-game core. Biggest unlock, lowest risk, immediately useful.
   Do this next; treat it as its own focused effort with a task breakdown.
2. Pause and evaluate against the public goal.
3. **Phase 2** — accounts, only if going public.
4. **Phase 3** — hardening, before any real public launch (with a security review).

**Recommended immediate next step:** scope Phase 1 into a concrete task list and
implement it behind the existing single-admin login, verifying isolation with two
parallel test games before touching accounts or public exposure.
