# Transcription Library Downloaded-Only Design

## Outcome

Studio Hub's Model Library shows transcription models only when at least one
registered machine has actually downloaded them. Other model modalities keep
their existing catalogue behavior.

## Scope

- Filter the Model Library's rendered rows, not `/api/hub/models` or the cached
  transcription inventory.
- Preserve uncached transcription entries for download planning, auditing,
  capability evidence, and backend consumers.
- Apply the rule whether the operator is viewing all modalities or filtering to
  Transcription.
- Keep machine, search, availability, and sorting controls unchanged.
- When the resulting table is empty, say `No downloaded transcription models.`
  when the active modality filter is Transcription; otherwise retain the
  general `No models match` message.

## Verification

- A behavioral frontend test executes the real library filtering/rendering
  logic with one downloaded and one uncached transcription model and proves
  only the downloaded row is shown.
- The same test proves an uncached non-transcription row remains visible.
- Existing frontend, release metadata, JavaScript syntax, and full repository
  tests remain green.
- No fleet update, model download, worker request, or backend API change occurs.
