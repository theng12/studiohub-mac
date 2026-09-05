# TASK-002 — Voice job recovery completion report

## Outcome

Studio Hub v2.20.0 preserves an accepted Voice Studio job as uncertain after a
Hub restart or transport loss, reconciles the original durable worker job, and
never submits accepted work again automatically. Normal recovery drains Voice,
requests cancellation of the exact job, and reports unresolved work for manual
action. Forced recovery remains an explicit path and restarts only the verified
local Voice launchd server/watchdog pair after the other-job safety checks pass.

The recovery trail remains durable and fenced by item and service. Original
assets are adopted once, unresolved restarts keep the Voice service drained,
and malformed or ambiguous worker job lists prevent forced recovery.

## Final review fixes

- `verified_voice_service()` catches the standard-library
  `xml.parsers.expat.ExpatError` raised by a valid XML header with a truncated
  plist and treats the service as unavailable. Its regression was proven
  red/green by removing and restoring only that catch.
- The Voice recovery UI now selects `#voice-job-action-status`, so the result of
  **Stop and reconcile** is visible to the operator. Its regression was also
  proven red/green.

## Verification

| Gate | Result |
| --- | --- |
| Focused recovery/qualification/broker/activity/fleet/frontend suite (310 tests) | Exit 0; log: `/Users/thengmacmini/pinokio/_handoffs/2026-09-05_studiohub-voice-recovery-ui/focused-tests.log` |
| Full `app/tests` suite (1510 tests collected) | Exit 0; one expected skip; log: `/Users/thengmacmini/pinokio/_handoffs/2026-09-05_studiohub-voice-recovery-ui/full-pytest.log` |
| Release metadata tests | 9 passed; log: `/Users/thengmacmini/pinokio/_handoffs/2026-09-05_studiohub-voice-recovery-ui/release-metadata.log` |
| Backend `compileall` | Exit 0 |
| `git diff --check` | Exit 0 |
| Release version | `VERSION`, changelog, and dashboard release notes agree on `2.20.0` and `2026-09-05` |

## Mock browser evidence

The current `app/frontend/index.html` was served by a loopback-only standard
library fixture. No Hub, Voice service, fleet controller, provider, credential,
or customer-data endpoint was contacted.

- Desktop: `/Users/thengmacmini/pinokio/_handoffs/2026-09-05_studiohub-voice-recovery-ui/desktop.jpg`
- Mobile full page (390 × 844): `/Users/thengmacmini/pinokio/_handoffs/2026-09-05_studiohub-voice-recovery-ui/mobile.jpg`
- Mobile keyboard focus: `/Users/thengmacmini/pinokio/_handoffs/2026-09-05_studiohub-voice-recovery-ui/mobile-focus.jpg`

The fixture verified uncertain, cancel-requested, manual-error, and final-done
states. Cancel-requested work exposes only **Cancel**, never **Clear**. The
non-forced action is labeled **Stop and reconcile** and reports the unresolved
original job truthfully. At 390 px the page has no horizontal overflow; the
per-item table scrolls internally. Keyboard focus moved to **Restart Voice
service**, scrolling both recovery buttons fully into view and retaining a
visible focus ring.

## Preserved boundaries

No live Hub or Voice service was stopped or restarted. No fleet state,
production data, credentials, deployment, hosted compute, GitHub Actions, or
paid provider calls were used.
