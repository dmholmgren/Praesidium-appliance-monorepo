# Praesidium Trial Presentation — Scope v1.2 (Trial-Day / WiFi build cut)

**Builds on:** `Praesidium_TrialPresentation_Scope_v1.md` + `_v1_1_addendum.md` (full spec; this is the trimmed *what-we-build-now* cut)
**Date:** June 18, 2026
**Decision (Dennis):** For the upcoming trial we **live on courtroom WiFi** using the **existing depot presentation relay** ("current functionality"). Local-first + **Statio** hardware nodes are **deferred to a later (large) build session.**
**Doubles as:** the **demo build** — networked displays + pop-out show well on the WestWave roadshow with no hardware to haul.

---

## Posture

- Reuse the depot relay **as-is** (WebSocket, in-memory session, `/present/{token}`, QR). Net-new = the trial *surface* on top, not new transport.
- **Single-operator drivable** for trial day. Build the session **schema multi-user-ready**, but do **not** build the console fan-out / driver lock yet.
- Accept the WiFi dependency knowingly — and hedge it with the two insurance items below, which are the difference between "WiFi hiccup" and "disaster."

---

## Build now (the trial-day + demo core)

| # | Slice | Cut for now |
|---|-------|-------------|
| **0** | **Recon gate** (v1 §1) | Confirm depot relay contract + decide generalize-vs-sibling relay. **Runs this session.** |
| **2** | **Display Rack** | Provision networked QR displays **+ local pop-out second-monitor** (frameless `window.open()` + Fullscreen API; BroadcastChannel transport). Pop-out is the most reliable output even on WiFi. |
| **3** | **Presentation Center** | Staging board (drag-to-display + Bench), exhibit queue from the trial-exhibits register, **push / blank / hold**, right-click present-to-display / send-to-staging. |
| **4-trim** | **Markup (courtroom-essential only)** | callout/zoom · highlight · laser · redaction-cover · **saved annotation sets**. Skip the full parity toolbar (arrows/shapes/tear-out/stamps/etc.) for now. |
| **mark** | **Mark + self-writing exhibit log** (useful half of v1 §7/§8) | Sequential numbering + synchronous DMS write-back so the exhibit index writes itself. |

**Insurance (keep precisely because we're on WiFi):**
- **Go-bag export** — one click to a static PDF binder + folder of display-ready images to a USB stick. If the network drops mid-cross, fall back to the stick. Pure anti-Logickull.
- **Instant-blank panic key** — one keypress clears the jury feed; touches no network. Logs the blank (and objection/ruling if entered) to the audit.

---

## Defer → Statio phase (the large later session)

Local-first / offline bundle · Statio hardware display nodes · multi-user console fan-out + driver soft-lock · judicial override · AI callout/designation surfacing · full parity annotation toolbar · depo-clip impeachment + synced video *(pull forward only if this trial needs video — flag if so).*

---

## Open decisions that gate the now-build

1. **Role assignment at join:** one QR + device self-selects role, or one QR per role? (Per-role is more foolproof in a live courtroom.)
2. **Annotation persistence default:** ephemeral-first (clears on push) or sticky-until-cleared?
3. **Does this trial use video depo clips?** If yes, pull the clip slice into the now-build; if no, it stays deferred.

(1)–(2) gate Slice 3/4; (3) decides whether the video slice moves up.

---

## Session protocol

```
ChatPrompts: v18.5 (carry)
Active build: Trial Presentation v1.2 (Trial-Day / WiFi cut)
Slice 0 recon: running this session
Relay decision: pending recon (generalize presentation_ws.py vs sibling)
Defer: Statio / local-first (separate large session)
```
