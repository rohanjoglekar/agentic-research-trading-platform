# Architecture — consolidated production deployment

This architecture document describes how the platform's research, trading,
application, inference, networking, and deployment components operate together
on a single four-core, 8 GB Linux VPS. It maps the service topology, resource
priorities, serialized deployment path, and boundary between the authenticated
dashboard and its public read-only build. The design treats resource
contention as a first-class constraint so priority-sensitive trading workloads
remain deterministic on shared hardware.

Most of this was written in August 2026, when the BTC research lab was the
server's main workload. That lab was paused at the end of that month and its
source is not in this repository. The equity research, options research and
the two paper books, whose source is here, run on the same server as scheduled
jobs.

## Production topology

```mermaid
%%{init: {"theme": "base", "themeVariables": {"fontSize": "17px", "lineColor": "#64748b", "edgeLabelBackground": "#ffffff"}, "flowchart": {"nodeSpacing": 30, "rankSpacing": 40, "curve": "basis"}}}%%
flowchart TB
    subgraph research["01 · RESEARCH + PROMOTION"]
        direction LR
        sources(["MARKET + MODEL<br/>INPUTS"]):::source --> engine["GENERATE +<br/>BACKTEST"]:::researchNode
        engine --> evidence["VALIDATE +<br/>SHADOW"]:::evidence
        evidence --> proposal(["VERSIONED<br/>PROPOSAL"]):::proposal
    end

    subgraph execution["02 · CONTROLLED EXECUTION"]
        direction LR
        approval{"HUMAN APPROVAL<br/>+ RUNTIME GATES"}:::gate --> books["INDEPENDENT<br/>PAPER BOOKS"]:::executionNode
        books <--> venues(["BROKER +<br/>VENUE APIS"]):::external
    end

    subgraph product["03 · PRODUCT + OBSERVABILITY"]
        direction LR
        state["VERSIONED RESULTS<br/>+ JOURNALS"]:::state --> api["FASTAPI<br/>READ MODELS"]:::api
        api --> views(["PRIVATE OPS<br/>+ PUBLIC VIEW"]):::view
    end

    proposal --> approval
    books --> state
    ops(["SYSTEMD · GOVERNOR · MONITORING"]):::ops -.-> engine
    ops -.-> books
    ops -.-> api

    classDef source fill:#fff7ed,stroke:#f59e0b,color:#7c2d12,stroke-width:2px,font-weight:700;
    classDef researchNode fill:#111827,stroke:#f59e0b,color:#f9fafb,stroke-width:2px,font-weight:700;
    classDef evidence fill:#ecfdf5,stroke:#059669,color:#064e3b,stroke-width:2px,font-weight:700;
    classDef proposal fill:#fffbeb,stroke:#d97706,color:#78350f,stroke-width:2px,font-weight:700;
    classDef gate fill:#fef3c7,stroke:#b45309,color:#78350f,stroke-width:2px,font-weight:700;
    classDef executionNode fill:#eff6ff,stroke:#2563eb,color:#1e3a8a,stroke-width:2px,font-weight:700;
    classDef external fill:#f8fafc,stroke:#64748b,color:#334155,stroke-width:2px,font-weight:700;
    classDef state fill:#f1f5f9,stroke:#475569,color:#1e293b,stroke-width:2px,font-weight:700;
    classDef api fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e,stroke-width:2px,font-weight:700;
    classDef view fill:#ecfeff,stroke:#0891b2,color:#164e63,stroke-width:2px,font-weight:700;
    classDef ops fill:#111827,stroke:#94a3b8,color:#f8fafc,stroke-width:2px,font-weight:700;
    style research fill:#fffaf0,stroke:#f59e0b,stroke-width:2px,color:#7c2d12
    style execution fill:#f8fafc,stroke:#334155,stroke-width:2px,color:#111827
    style product fill:#f0f9ff,stroke:#0284c7,stroke-width:2px,color:#0c4a6e
    linkStyle default stroke:#64748b,stroke-width:2px;
```

The diagram is intentionally architectural rather than source-level. It shows
runtime responsibilities, promotion flow, and trust boundaries, not package
structure, and it was drawn for the research lab's promotion pipeline.

## The service map

- `forge-btc-lab-daemon` — Until it was paused, continuously performed
  strategy generation, backtesting, admission to live order-free shadow
  testing, and promotion accounting at low CPU priority (`nice 15`).
- `forge-knn-perp` and the binary-market shadow job — Generate live-market
  predictions (the perpetual runner on each completed hourly bar, the shadow
  job every 15 minutes) and occupy the execution path closest to real capital.
- Scheduled jobs (cron and systemd timers) — The morning options scan, the
  options trader three times each trading day, the equity trader three times
  each trading day, its weekly review, the weekly AI reads, and the
  valuation-model batches with their half-hourly collector.
- `forge-dashboard-api` (:8700) and `forge-dashboard-web` (:3000) — Serve the
  FastAPI and Next.js private dashboard behind nginx Basic Authentication.
- `forge-view` (:3001) — Serves the public demonstration, a separate Next.js
  app compiled in view-only mode and placed behind a GET-only gateway. Vercel
  serves the same app as the canonical public front.
- Ollama — Provides local inference for research review and generated
  summaries. The runtime permits one loaded model and disables swap
  (`MemorySwapMax=0`) because memory paging makes local inference operationally
  slower than omitting it.

## Priority-aware CPU throttling

The research daemon must yield capacity to live trading, the API, and model
loading. Its original throttle used the one-minute load average. Seven days
of observation showed that this signal was unsuitable: system load never fell
below 4.0, and the daemon yielded 307 times in 5¼ hours, leaving it idle for
73% of elapsed time. Linux load average is insensitive to process priority;
because approximately 74% of CPU consumption came from `nice 15` research,
the daemon repeatedly interpreted its own low-priority workload as contention.

The replacement reads `/proc/stat` and calculates the fraction of total CPU
capacity consumed by **normal-priority** work. This isolates the live trader,
API, and Ollama's approximately 281% model-load spikes from low-priority
research processes:

```python
# algo-trading/btc_lab/loop.py (docstring abridged)
def _nonnice_busy_frac(window_s: float) -> float | None:
    """Fraction of TOTAL cpu capacity spent on NON-NICE work over `window_s`.

    Why not the 1-minute load average, which this replaces: loadavg is
    nice-BLIND. On this box ~74% of CPU is `nice` — overwhelmingly the daemon's
    own nice-15 peers ... So `load1 > ncpu × frac` was a tautology: the daemon
    yielded because research was running, which is the thing it was trying
    to do."""
```

With the change the daemon no longer counts its own low-priority work as
contention. The implementation retains the one-minute load average as a
degraded fallback if `/proc/stat` cannot be read, ensuring that monitoring
failure does not disable throttling entirely.

## Serialized deployment pipeline

Every push to `main` executes smoke tests through GitHub Actions before
connecting to the server and invoking `infrastructure/deploy.sh`. The script
resets the checkout to the tip of `main`, reasserts nginx authentication
and routing invariants, restarts services affected by changed paths, and
rebuilds the public application. Two controls originated from production
incidents:

- **Mutual exclusion.** An automated deployment and a manual build once ran
  `npm` concurrently against the same working tree, corrupting the dependency
  installation. The deploy script now takes a `flock` lock before modifying
  build state, and a manual build is expected to take the same lock.
- **Tracked-artifact verification.** For ten days, the public application
  repeatedly failed after deployment with `next: not found` despite clean
  dependency rebuilds. The root cause was a tracked `node_modules` symlink to
  an absolute path on the development Mac. The `.gitignore` entry
  `node_modules/` matched directories but not the symlink, allowing it into
  version control; every `git reset --hard` then restored the dangling
  `/Users/...` target over the server's newly installed dependencies. Removing
  the path from the Git index resolved the incident. The resulting operational
  rule is explicit: when a deployment repeatedly removes an artifact, inspect
  the repository's tracked object type before diagnosing the package manager.

## A separate public application

The public application is a separate Next.js app built from a hand-synced copy
of the dashboard's components. A compile-time `VIEW_ONLY` flag keeps mutating
controls out of everything it renders. Fixes are made in the dashboard and
copied across by hand, which has let a stale copy ship a wrong label before.
The `/demo` pages are their own components, and their source is in this
repository under `forge-view/`. The public landing page reads from a
dedicated `/api/landing` endpoint that returns under 2 KB of aggregate counts
and downsampled sparkline data; the research payload behind the full section
is about 3 MB. Static
content renders independently, while live values appear only after a
successful response, preventing transient API failures from displacing the
page with loading or error states.

## Public disclosure boundary

This document describes the control flow needed to evaluate the engineering:
how research becomes evidence, how evidence becomes an eligible strategy, how
eligible strategies reach paper execution, and how production state reaches
the authenticated and public interfaces. The source for the equity research,
options research and both paper books is in this repository. The API server,
everything that reads the brokerage account, credentials, internal hostnames,
mutable control endpoints, operational recovery commands and the research
lab's own source are not. The short excerpts quoted from that lab explain a
safety or statistical property and are not enough to reproduce it.
