# Patent-Workbench → VPS Migration Plan (future roadmap, not for immediate execution)

## Context

The patent-workbench stack (patent-bench app + nlm-keeper) runs on a Docker-Desktop-on-WSL2 host that has suffered **9 memory-related crashes/wedges in one week**, each killing multi-day NLM sweeps mid-round. The sweeps self-recover, but every incident costs hours of quota-window time, and the host's ~7.8 GB VM is chronically at 60–70% swap. Moving the stack to a VPS puts the multi-day autonomous work (sweeps, watchers, audits) on stable rails, independent of the local machine. User decisions (2026-08-21): **hybrid-first migration** (app+data first, Google browser sessions later), **Claude Code installed on the VPS** for model-call auth, **Hetzner ~€15–20/mo** tier.

Measured facts: volumes to move ≈ 2.1 GB (`patent-bench-data` 1.48 GB, `nlm-keeper-chrome` 611 MB, `nlm-profile` 87 KB). App peak RSS 3–4 GB (WEB_CONCURRENCY=1), keeper 0.5–1 GB (3 Chromiums). SQLite single-writer at `/data/workbench.db` — must live on VPS-local NVMe, never network storage. The app has **no authentication** — nothing is ever exposed publicly.

**Key architectural fact discovered in design:** even in hybrid mode, the app's NotebookLM CLI calls originate **from the VPS IP** (cookies in the shared `nlm-profile` volume). Hybrid only keeps the *browser* sessions (cookie refreshers) local. Therefore the decisive test of the whole plan is the **cookie-canary**: do NLM CLI calls with transplanted cookies work from a datacenter IP? The keeper's boot-quarantine mechanism (`keeper/entrypoint.sh` — browsers boot Google-blocked) lets us run this test before any Chromium ever touches Google from the new IP.

## Phase 0 — Decisions taken + prerequisites

- **Mode: hybrid-first.** Stage A: patent-bench + data + Claude bridge on VPS; keeper stays local, cookies rsynced local→VPS every 2–3 min over Tailscale. Stage B (gated on 2 clean weeks + user go): move keeper, re-login accounts at the VPS one at a time.
- **Claude auth: Claude Code on the VPS** (own OAuth in tmux, seeds `/seed` for the app's entrypoint — same subscription, self-maintaining). Bootstrap during Phase 3 via one-time push of `.credentials.json` from local; retire the push once VPS-native login works. Emergency lever (no code change): `ANTHROPIC_API_KEY` env — `claude_bridge` shells out to the `claude` CLI which honors it.
- **Provider/size: Hetzner CPX41-class** — 8 vCPU / 16 GB RAM / 240 GB NVMe (~€15–20/mo, EU). 16 GB because the migration motive is memory death; workload needs ~5–6 GB working set. +8 GB swap file (swappiness 10). Backup target: Hetzner Storage Box or Backblaze B2 (~€1–5/mo). **Total ≈ €16–25/mo.**
- Prereqs: freeze current env overrides into `.env` (WEB_CONCURRENCY, PB_AUTO_FIGURES, PB_REDUCE_TIMEOUT, NLM_QUERY_TIMEOUT, PROFILE_ALIASES, REFRESH_SECS); record the pre-migration `audit_status.py` gate matrix + deploy head as baseline; stage recovery phones/backup codes for all 3 Google accounts (challenges at a new IP are expected in Stage B).

## Phase 1 — Provision + harden

1. Debian 12 / Ubuntu 24.04 minimal; non-root `deploy` user in `docker` group; SSH key-only, no passwords; fail2ban.
2. Docker CE + compose plugin (NOT Desktop). `/etc/docker/daemon.json`: `log-driver json-file, max-size 50m, max-file 3` (plain Docker doesn't rotate — keeper/Chromium are chatty).
3. **Tailscale** on VPS + local machine: stable addressing, encrypted access; ufw default-deny inbound, allow tailscale0 (+22 temporarily, close after Tailscale is proven). All service ports bind `127.0.0.1` on the VPS — reached only via tailnet/SSH tunnel. noVNC especially (unauthenticated remote control of logged-in Google sessions).
4. Swap file 8 GB; `unattended-upgrades` security-only with **auto-reboot disabled** (sweeps run for days — kernel reboots happen manually in quota-pause windows); NTP.
5. Alerts: lightweight cron (disk >80%, mem >90%, containers down → ntfy.sh/Telegram) or Uptime-Kuma on the tailnet.
6. From the local dev container: `docker context create vps --docker "host=ssh://deploy@<tailnet-ip>"`; SSH `ControlMaster auto` + `ControlPersist 10m` in `~/.ssh/config` (kills the per-call handshake latency for stdin-piped audit scripts). Verify `docker --context vps ps`.

## Phase 2 — Stack bring-up (compose replaces both serve.sh scripts)

New `deploy/docker-compose.yml` (encodes `scripts/serve.sh` + `keeper/serve.sh` semantics):

- `patent-bench`: build from `deploy/Dockerfile`; `container_name: patent-bench` (all existing `docker exec` tooling matches verbatim); ports `127.0.0.1:8099:8000`; env from `.env`; volumes `patent-bench-data:/data`, `/opt/patent-workbench/claude-seed:/seed:ro` (replaces `/root/.claude`), `/opt/patent-workbench/skills:/skills-rw`, `nlm-profile:/home/app/.notebooklm-mcp-cli`; **`mem_limit: 6g`** (the WSL2 lesson: contain the container, never kill the host); `stop_grace_period: 30s`; `restart: unless-stopped`.
- `nlm-keeper` (Stage B only): `container_name: nlm-keeper`; ports `127.0.0.1:8106:6080`; `shm_size: 1g`; **`PB_URL: http://patent-bench:8000`** (compose DNS — `host.docker.internal` doesn't exist on plain Linux Docker); volumes `nlm-profile`, `nlm-keeper-chrome`; **`stop_grace_period: 25s`** (preserves the graceful-stop cookie-flush guarantee — never `docker rm -f` the keeper); `mem_limit: 2g`.
- All three volumes `external: true` — pre-created and tar-restored in Phase 3 *before* first `up` (avoids the image's VOLUME initializer racing the restore).
- Keeper watchdog cron (Stage B): restart keeper on Chromium RSS/zombie growth, but **only when no sweep is in an NLM-active phase** (check `/api/tabs/N/claims-audit/status` first).
- Exit gate: `compose up` with empty volumes → app serves on 8099 with graceful "no /seed credentials / no NLM profile" warnings.

## Phase 3 — Data migration + the decisive cookie-canary (Stage A)

Ordering: **park sweeps → freeze → tar → transfer → restore → CLI cookie-canary → hybrid cookie-sync → Claude seed.**

1. Park all sweeps locally (quota-pause or stop; `running:false, resumable:true` per tab); kill in-container sweep-watcher.
2. Freeze: `docker stop -t 15 nlm-keeper` FIRST (cookie flush), then `docker stop patent-bench` (SQLite quiescent — no WAL hazards).
3. Tar the 3 named volumes (`docker run --rm -v <vol>:/v -v <hostdir>:/out alpine tar czf ...`), checksum, transfer over Tailscale, restore into pre-created external volumes on the VPS, then **`chown -R 1000:1000`** via a root alpine container (root-owned-profile failures bit twice locally — explicit checked step).
4. **Cookie-canary (go/no-go for the whole plan):** start patent-bench only (no keeper anywhere touching Google yet beyond the local one). Per account: `docker exec patent-bench <nlm-cli> notebook list --profile <default|drawnformula|work2>` from the VPS IP. Cookies are ~5 min stale — do steps 2–4 in one sitting. **Pass** → proceed. **Fail for ≥2 accounts** → datacenter IP unusable for NLM: swap IP/provider (cheap before commitment) or abandon VPS-for-NLM (app could still move; sweeps stay local).
5. Hybrid cookie-sync: restart local keeper; add a 2–3 min rsync (local `nlm-profile` → VPS volume via Tailscale) + reverse `PB_URL` (local keeper's auto-resume calls go to the VPS app over the tailnet). Accept the ~5 min staleness window as Stage-A cost.
6. Claude: install Claude Code on the VPS (tmux), OAuth login; point `/opt/patent-workbench/claude-seed` at its credentials (or a 15-min `docker cp` refresher cron); restart bench; confirm entrypoint "seeded claude credentials" + one smoke chat/extract call.
7. Resume sweeps ON THE VPS; re-arm sweep-watcher (`docker cp` + `docker exec -d` — commands unchanged).

## Phase 4 — Verification (the repo's own deterministic gates, run through the docker context)

1. `docker --context vps exec -i patent-bench python3 - < scripts/audit_staging.py` — S3 live notebook inventory per account = the definitive "NLM sessions work from the VPS" test; S1/S4 = DB integrity post-transfer.
2. `audit_status.py --deploy-head ...` — gate matrix must reproduce the Phase-0 baseline (watermarks unchanged since freeze).
3. **1-round sweep canary per account** (one resumed round each): staging + query round-trip, quota accounting, keeper auto-resume path (restart keeper mid-round once, expect auth-pause → auto-resume).
4. Latency check of stdin-piped audits over the SSH context (<1 s overhead with ControlMaster; else run scripts from a VPS-side checkout).
5. Green criterion: gate matrix ≥ baseline, one clean round per account, no Google auth events for 24–48 h.

## Phase 5 — Stage B (keeper to VPS) + cutover + ops routine

- **Stage B gate:** 2 clean weeks of Stage A + user go. Then move `nlm-keeper-chrome`, `compose up nlm-keeper`, and re-login accounts via noVNC over the tailnet **one at a time, least-valuable first (work2), 24 h soak between accounts** — a session created *at* the VPS IP is the healthiest end state. Challenge ladder: retry with recovery phone → unflag from residential IP then retry → per-account hybrid stays → all flagged = permanent hybrid.
- **Cutover** is administrative (no public endpoint): VPS declared primary; local stack stopped but volumes kept as cold standby (never deleted); local dev container defaults `docker context use vps`.
- **Backups (nightly cron, VPS):** `sqlite3 .backup` via docker exec (safe against the single writer) + restic/borg of the three volumes + the .backup file → Storage Box/B2 and/or restic-pull to the local machine; 7 daily + 4 weekly; **monthly restore drill** (scratch volume + `PRAGMA integrity_check`).
- Routine: weekly guarded `docker system prune`; keeper watchdog; credential-staleness alert (>12 h); monthly manual reboot in a quota-pause window; deploys via `docker --context vps compose up -d --build`.

## Rollback (valid until local volumes are deleted — never delete them)

- Phases 1–4 failure: restart local stack via existing serve.sh scripts — nothing diverged.
- After VPS accumulated sweep data: park VPS sweeps → tar VPS volumes → restore over local → local keeper re-logins (sessions rarely survive two IP jumps) → local audits green → resume. Keep local untouched for 2 weeks after cutover.

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Google rejects datacenter IP for NLM CLI calls | Medium–High (Hetzner ranges widely blocklisted) | Fatal for the plan | Phase-3 cookie-canary BEFORE any commitment; cheap IP/provider swap; Netcup/OVH alternates |
| Account challenge/lockout at Stage-B re-login | Medium | High per account | Recovery codes staged; one account at a time; least-valuable first; residential unflag path; per-account hybrid |
| Claude OAuth expiry on VPS | Low (VPS-native login self-maintains) | Medium | Staleness alert; API-key env as emergency lever |
| SQLite on network storage | design-excluded | Fatal | Hard rule: local NVMe named volume; `.backup` API only |
| Chromium leak OOMs VPS (WSL2 repeat) | Medium | High | mem_limits + swap + watchdog — container dies, host survives, restart policy recovers |
| Cookie loss on ungraceful keeper stop | Medium | Medium | compose stop_grace_period; never rm -f |
| Auto-update reboot kills multi-day sweep | Low–Med | Medium | auto-reboot off; resume-state + watcher are the net |
| Hybrid cookie staleness (Stage A) | Medium | Low–Med | 2–3 min rsync; keeper auto-refresh every 300 s; Stage B removes it |

## Critical files (source of truth for the compose conversion)

- `scripts/serve.sh` (bench ports/env/volumes), `keeper/serve.sh` (keeper env, graceful stop, PB_URL, aliases)
- `deploy/entrypoint.sh` (/seed seeding + lock clearing to preserve), `keeper/entrypoint.sh` (boot-quarantine the canary sequencing relies on)
- `scripts/audit_staging.py` + `scripts/audit_status.py` (post-migration verification gates)
- `scripts/reseed-nlm-profile.sh` (chown/chmod pattern for volume restores)

## Verification summary

End-to-end proof = Phase 4: all audits green through `docker --context vps`, one sweep round per account from the VPS IP, keeper auto-resume path exercised, 48 h without Google auth events — then and only then cutover.
