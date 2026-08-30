# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is the owner-operator of a private fleet of Apple Silicon Macs running KH Studio applications across multiple physical locations. They use one Controller Studio Hub to understand fleet health, supervise production, and maintain remote machines without visiting each Mac.

## Product Purpose

Studio Hub KH is the operational control plane for the KH Studio fleet. It discovers and monitors local and remote Studios, routes production work, preserves job evidence, manages shared resources, and gives the owner one trustworthy view of what every machine is doing.

Success means the owner can understand fleet state, identify available or troubled capacity, and take safe actions without interpreting raw process data or guessing from whether an application happens to be online.

## Positioning

Studio Hub combines local-first generation infrastructure with fleet-wide control. Each Mac can continue operating independently, while a Controller aggregates trusted status and history across the location through the same private Studio and Agent Hub APIs used for real work.

## Operating Context

- The fleet contains Macs with different Apple chips and unified-memory sizes.
- Production work may be dispatched through Studio Hub or started directly inside Image Studio and Voice Studio.
- Machines may be active, recently finished, ready for more work, idle for a long time, offline, updating, or in need of attention.
- Operators commonly connect over Tailscale and may not have physical access to every Mac.
- Updates and maintenance must not interrupt active work or weaken enrollment, authentication, or recovery safeguards.

## Capabilities and Constraints

- The current production fleet tracks Studio Hub, Image Studio, and Voice Studio.
- Live status must distinguish application health from actual generation activity.
- Direct Studio activity and Hub-dispatched activity both belong in operational statistics.
- Historical performance comparisons must compare like-for-like work and must not imply confidence when evidence is sparse.
- Existing throughput, model, and machine analytics remain useful and must be preserved beneath the live operational view.
- The feature must not change GenStudio-facing behavior or job routing.
- Older Studios that cannot report direct activity remain compatible and are labelled honestly instead of being guessed about.

## Evidence on Hand

- Studio Hub already polls every registered Studio's `/api/health` response and retains the complete health payload in memory.
- Hub-dispatched generation records already include machine, Studio, model, runtime, progress, and terminal state evidence.
- Image Studio and Voice Studio already maintain local generation job state; the missing piece is a small standardized health payload for direct activity.
- Studio Hub's asset ledger already powers the existing Stats page, including throughput and runtime summaries.

## Product Principles

- Explain the fleet in plain operational language before showing charts.
- Never confuse an online process with a working machine.
- Prefer direct observed evidence over inferred state.
- Keep every machine independently useful when the Controller is unavailable.
- Preserve safe, boring compatibility across mixed fleet versions.

