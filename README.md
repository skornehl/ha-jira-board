# Jira Board for Home Assistant

A live, drag-and-drop Kanban board for your Jira issues, right inside Home
Assistant - and it's a genuine **two-way sync**: dragging a card between
columns really transitions the underlying Jira issue via the REST API, it's
not a one-off import.

Each board column (`To Do` / `In Progress` / `In Review` / `Done`) is a
native `todo.*` entity, plus a small bundled Lovelace card
(`custom:jira-board-card`) that renders them as a real drag-and-drop board.
The card ships inside the integration and registers itself automatically -
no manual Lovelace resource to add.

## Installation (HACS)

1. HACS → the three-dot menu (top right) → **Custom repositories**
2. Add this repository's URL, category **Integration**
3. Install "Jira Board", then restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → search "Jira Board"

## Setup

You'll need a classic Atlassian API token (**not** an OAuth app token) -
create one at <https://id.atlassian.com/manage-profile/security/api-tokens>.

The config flow asks for:

| Field | Meaning |
|---|---|
| Jira base URL | e.g. `https://yourcompany.atlassian.net` |
| Email | your Atlassian account email |
| API token | the token from the link above |
| Projects | comma-separated project keys to pull issues from, e.g. `HA,FAM` |
| Default project | which project a *brand new* card typed directly on the board gets created in |

## Adding the board to a dashboard

```yaml
type: custom:jira-board-card
columns:
  - entity: todo.to_do
    title: To Do
  - entity: todo.in_progress
    title: In Progress
  - entity: todo.in_review
    title: In Review
  - entity: todo.done
    title: Done
```

## How the sync works

- **Poll → board**: every `scan_interval` seconds (default 120), the
  integration runs one JQL search across your configured projects and
  rebuilds each column's item list from the issues' current status.
- **Board → Jira**: dragging a card is implemented as removing it from the
  source column's `todo` list and re-adding it to the target column's list.
  The integration recognises this because every card's visible text is
  written as `"KEY  summary"` (e.g. `"HA-12  Fix the thing"`) - on re-add,
  it looks up the leading key, and if it already knows that issue from a
  *different* column, it calls Jira's transitions API to move it for real
  (the workflow's actual transition ID is resolved dynamically per issue,
  never hardcoded, since it varies by project/workflow).
- **New cards**: typing a new card with no recognisable `KEY` prefix creates
  a brand new Jira issue (type `Task`) in the configured default project.
- **Checking a card off**: marking an item complete (the checkbox, not a
  drag) transitions the issue straight to `Done`.
- **Deleting a card**: intentionally a no-op against Jira. A drag-move is
  implemented as delete-then-create with no reliable way to tell that apart
  from someone removing a card outright, and auto-closing an issue on a
  guess is worse than a card that reappears after the next refresh (nothing
  is ever silently lost - the coordinator's next poll is always the final
  source of truth). Delete the issue in Jira itself if you really mean it.

## Columns / workflow

Columns are currently fixed to `To Do`, `In Progress`, `In Review`, `Done`
(`const.py: COLUMNS`) - the union of statuses seen across the projects this
was built against. If your project's workflow doesn't offer one of these as
a valid transition target from a given issue's current state, Jira will
reject the move and the card snaps back on the next refresh. Editing
`COLUMNS`/`COLUMN_SLUGS` in `const.py` to match your own workflow is
currently a fork-it-yourself change, not a config option.

## Known limitations

- Single Jira Cloud site per config entry (add the integration again for a
  second site).
- No sync of summary/description edits after creation, no due dates,
  priority, or assignee - status/column only.
- `Done` issues older than 14 days drop off the board (`const.py:
  DONE_RETENTION_DAYS`) so it doesn't accumulate forever.

## License

MIT, see [LICENSE](LICENSE).
