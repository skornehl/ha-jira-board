# Jira Board for Home Assistant

A live, drag-and-drop Kanban board for your Jira issues, right inside Home
Assistant - and it's a genuine **two-way sync**: dragging a card between
columns really transitions the underlying Jira issue via the REST API, it's
not a one-off import.

Each board column (`To Do` / `In Progress` / `In Review` / `Done`) is a
native `todo.*` entity, plus a small bundled Lovelace card
(`custom:jira-board-card`) that renders them as a real drag-and-drop board.
The card ships inside the integration and registers itself automatically
for browser use - no manual Lovelace resource needed there.

## Installation (HACS)

1. HACS → the three-dot menu (top right) → **Custom repositories**
2. Add this repository's URL, category **Integration**
3. Install "Jira Board", then restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → search "Jira Board"

### "Configuration error" in the Companion App

The auto-registration above uses `add_extra_js_url`, which loads fine in a
browser but has been observed *not* loading in the iOS/Android Companion
App's WebView, showing a bare "Configuration error" with no further detail
on the card instead. If that happens, add the card as an explicit Lovelace
resource once (this is the same mechanism every other bundled/HACS card in
a Home Assistant install typically uses, and works reliably everywhere
including the Companion App):

Settings → Dashboards → ⋮ (top right) → **Resources** → **Add Resource**
→ URL: `/jira_board_static/jira-board-card.js` → Resource type: **JavaScript
Module**.

## Setup

You'll need a classic Atlassian API token (**not** an OAuth app token) -
create one at <https://id.atlassian.com/manage-profile/security/api-tokens>.

The config flow is three steps:

1. **Credentials** - Jira base URL (e.g. `https://yourcompany.atlassian.net`),
   your Atlassian account email, and the API token from the link above.
2. **Projects** - a multi-select list of every project your account can see,
   fetched live from Jira (nothing to type/guess) - pick which ones the
   board should pull issues from.
3. **Default project** - which of the projects picked in step 2 a *brand
   new* card typed directly on the board gets created in.

To change the tracked projects, the default project, or **rotate the API
token**, use the integration's **Configure** button (Settings → Devices &
Services → Jira Board → Configure) - two steps: credentials (leave the
token field blank to keep the current one), then the same live project
picker as initial setup. Takes effect immediately, no restart or
re-adding needed.

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
# Optional:
group_by_epic: false        # start grouped into swim lanes, one per Epic
project: HA                 # default filter - handy for one dashboard tab per project
# projects: [HA, FAM]        # (or a fixed set instead of a single default)
card_id: my-board            # explicit key for the persisted UI state (see below);
                              # auto-derived from columns/project if omitted
```

## Card features

- **Drag and drop** between columns moves the card instantly (optimistic
  update - the visual move happens immediately, the actual Jira sync runs
  in the background and reconciles on the next poll if anything goes
  sideways).
- **Sorted** ascending by issue number within each column (`HA-9` before
  `HA-21`), project key as the primary sort key.
- **Group by Epic** toggle in the toolbar turns the single row of columns
  into swim lanes, one per Epic plus a "Kein Epic"/"No Epic" catch-all.
  Purely a client-side layout choice - drag-and-drop works exactly the same
  across lanes. Epics get a lane even with zero cards currently on the
  board (e.g. a freshly created one) - the integration fetches the full
  Epic list for the configured project(s) separately, not just the ones
  inferable from issues actually on screen.
- **Project filter** dropdown, defaulting to the `project`/`projects`
  config above. Set a different default per dashboard tab to get one board
  per project.
- **Search box** filters cards live as you type (matches ticket key and
  text). In Group by Epic view, a lane only stays visible if at least one
  of its cards matches - the Epic itself doesn't need to match, only
  something inside it. Not persisted across reloads on purpose.
- The Epic toggle and the project filter are **remembered** across page
  reloads and HA restarts (`localStorage`, scoped per card instance); the
  search box intentionally isn't.
- **Click a card** to open a details popup - summary, status, project,
  priority, assignee, reporter, labels, created/updated dates, and the
  full description (rendered the same as Jira shows it), plus a link to
  open the issue directly in Jira. Fetched live on click (not cached from
  the board's own poll, which only carries the handful of fields the board
  itself needs), so it's always current. A drag-and-drop move doesn't
  trigger it - only a plain click.
- **"+ Aufgabe hinzufügen"** input at the bottom of every column creates a
  brand new Jira issue directly from the board (see below for which
  project it lands in). Typed inside a specific Epic's lane (Group by
  Epic view), the new issue is linked to that Epic from the start
  instead of landing in "Kein Epic" - typed in the ungrouped view or in
  the "Kein Epic" lane itself, it's created without one, same as before.

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
  a brand new Jira issue (type `Task`). It lands in whichever *single*
  project the card's filter is currently set to, or the config's
  `default_project` if the filter is on "Alle"/"All".
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
- The toolbar (search, project filter, group-by-epic toggle) scrolls away
  with the rest of the board on a tall view instead of staying pinned -
  a "sticky" version was tried and reverted (see git history around
  1.7.1-1.7.4) after it repeatedly failed to work correctly in a Panel
  view and once broke the "add task" input.
- No sync of summary/description edits after creation, no due dates -
  status/column only. The details popup can *show* description, priority,
  assignee, reporter and labels (read-only, fetched from Jira live), but
  nothing on the board writes any of those back.
- `Done` issues older than 14 days drop off the board (`const.py:
  DONE_RETENTION_DAYS`) so it doesn't accumulate forever.

## License

MIT, see [LICENSE](LICENSE).
