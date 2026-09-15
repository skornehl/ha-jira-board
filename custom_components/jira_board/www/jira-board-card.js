// Jira Board Card - a small, dependency-free Kanban card for the
// jira_board integration's todo.* column entities.
//
// Deliberately plain vanilla Web Components (no lit/react/build step): the
// integration bundles and serves this file itself, so it has to work as-is
// straight out of the Python package with zero npm/build tooling.
//
// Drag a card between columns to move it: on drop we call todo.remove_item
// on the source entity and todo.add_item on the target entity with the
// exact same summary text. The backend (todo.py) recognises the "KEY  "
// prefix in that text to tell a real move apart from a brand new card -
// summary text must be passed through unchanged for that to work.
//
// "Group by Epic" turns the single row of columns into swim lanes, one per
// Epic (plus a catch-all "Kein Epic" lane). This is purely a client-side
// layout choice on top of the same underlying entities - dragging a card
// still only ever calls remove_item/add_item on the two status-column
// entities involved, regardless of which lane it visually sits in. Epic
// info rides along in each TodoItem's `description` field as a small JSON
// blob (`{project, epic_key, epic_name}`), since TodoItem has no field of
// its own for it.

const NO_EPIC = "__no_epic__";

class JiraBoardCard extends HTMLElement {
  setConfig(config) {
    if (!config.columns || !Array.isArray(config.columns)) {
      throw new Error(
        "jira-board-card: 'columns' is required, e.g. " +
          "[{entity: 'todo.to_do', title: 'To Do'}, ...]"
      );
    }
    this._config = config;
    // Persist the "Group by Epic" toggle and the live project filter
    // across page reloads *and* HA restarts (both are just this browser
    // reloading the dashboard from HA's perspective) via localStorage -
    // scoped per card instance so separate "one tab per project" boards
    // don't clobber each other's remembered state. Prefer an explicit
    // `card_id` in the config; falls back to a key derived from the
    // configured columns/project so it's still stable without one.
    this._storageKey =
      "jira-board-card:" +
      (config.card_id ||
        JSON.stringify({
          cols: config.columns.map((c) => c.entity),
          project: config.project || config.projects || null,
        }));
    const saved = this._loadPersisted();

    this._groupByEpic = saved?.groupByEpic ?? !!config.group_by_epic;
    // Accept either `project: "HA"` or `projects: ["HA", "FAM"]` as the
    // *default* filter selection - still changeable live via the toolbar
    // dropdown, and remembered from there on (see above). Handy for
    // setting up one dashboard tab per project by giving each tab's card
    // a different default.
    const configured = config.projects || (config.project ? [config.project] : null);
    const configDefault = configured && configured.length === 1 ? configured[0] : "__all__";
    this._projectFilter = saved?.projectFilter ?? configDefault;
    // Which Epic lanes are collapsed (by key) - remembered the same way
    // as the two above, so a board with many Epics stays the way you left
    // it across reloads instead of re-expanding everything every time.
    this._collapsedLanes = new Set(saved?.collapsedLanes || []);

    this._dragItem = null;
    this._itemsByEntity = {};
    // Deliberately not persisted (unlike groupByEpic/projectFilter above) -
    // a stale search term silently still filtering the board after a
    // reload would be far more confusing than useful.
    this._searchQuery = "";
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    // `hass` is reassigned on *every* state change anywhere in the whole
    // instance, not just our 4 entities - re-fetching all of them via
    // todo.get_items on each tick was hammering the WS connection and is
    // almost certainly why moves felt like they took ~30s (a backlog of
    // redundant overlapping fetches queuing up, not the actual Jira
    // round-trip). Only refetch when one of *our* entities' state (item
    // count) actually changed, and never run two fetches concurrently -
    // if a relevant change arrives mid-fetch, just remember to run once
    // more right after instead of overlapping.
    if (!this._config) return;
    // Item count alone (`state`) misses a change that's *only* in
    // `all_epics` - e.g. a brand new Epic with no cards yet wouldn't
    // otherwise get its lane rendered until some unrelated item move
    // happened to also trigger a refetch. Cheap to compare, the list is
    // always tiny.
    const seenKey = (s) => (s ? `${s.state}|${JSON.stringify(s.attributes?.all_epics || [])}` : undefined);
    const changed = this._config.columns.some((col) => {
      const s = hass.states[col.entity];
      return s && seenKey(s) !== this._lastSeenState?.[col.entity];
    });
    if (!changed) return;
    this._lastSeenState = {};
    for (const col of this._config.columns) {
      this._lastSeenState[col.entity] = seenKey(hass.states[col.entity]);
    }
    if (this._fetching) {
      this._pendingRefetch = true;
      return;
    }
    this._updateItems();
  }

  _loadPersisted() {
    try {
      const raw = localStorage.getItem(this._storageKey);
      return raw ? JSON.parse(raw) : null;
    } catch (err) {
      return null;
    }
  }

  _savePersisted() {
    try {
      localStorage.setItem(
        this._storageKey,
        JSON.stringify({
          groupByEpic: this._groupByEpic,
          projectFilter: this._projectFilter,
          collapsedLanes: [...this._collapsedLanes],
        })
      );
    } catch (err) {
      // Private browsing / storage disabled / quota full - not worth
      // surfacing an error over, the toggle just won't stick this time.
    }
  }

  getCardSize() {
    return this._groupByEpic ? 10 : 6;
  }

  connectedCallback() {
    if (!this.shadowRoot) this._render();
    // Safety net: the normal path only re-fetches when the `hass` setter
    // sees one of our 4 entities' state/all_epics actually change - fast
    // and quiet, but a single stuck `_fetching` guard (e.g. a `callWS`
    // that never resolves/rejects) would silently freeze the board
    // forever, since every future real change just piles into
    // `_pendingRefetch` and waits for a `_fetching` reset that never
    // comes. Poll unconditionally every 20s as a backstop so a reload is
    // never required to see it self-correct - cheap (a handful of
    // todo.get_items calls), and _updateItems() already no-ops safely if
    // hass/config aren't ready yet.
    if (!this._pollInterval) {
      this._pollInterval = setInterval(() => {
        // Also the actual unstick: if we've been "fetching" for longer
        // than one whole poll cycle, something hung - force it back open
        // rather than let _pendingRefetch queue up against a guard that
        // will never clear itself.
        if (this._fetching) this._fetching = false;
        this._updateItems();
      }, 20000);
    }
  }

  disconnectedCallback() {
    if (this._pollInterval) {
      clearInterval(this._pollInterval);
      this._pollInterval = null;
    }
    if (this._escKeyHandler) {
      document.removeEventListener("keydown", this._escKeyHandler);
      this._escKeyHandler = null;
    }
  }

  _render() {
    const root = this.attachShadow ? (this.shadowRoot || this.attachShadow({ mode: "open" })) : this;
    root.innerHTML = `
      <style>
        /* A "sticky toolbar while scrolling" feature was attempted here
           across three released versions (1.7.1-1.7.3: position: sticky,
           then overriding ha-card's overflow, then a full flex/height
           self-scrolling layout) - all three broke or failed to fix
           anything, and the last one broke the "add task" input in some
           columns on top of that. None of it could be visually verified
           in this environment (no browser/devtools access), so rather
           than keep guessing blind at HA's internal card-layout mechanics,
           this reverts cleanly to the plain, known-working layout from
           before that attempt. The toolbar again simply scrolls away with
           the rest of the board's content - see the README's "Known
           limitations" section.
        */
        :host { display: block; }
        ha-card { padding: 12px; }
        .toolbar {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 10px;
          font-size: 0.9em;
          color: var(--primary-text-color);
        }
        .toolbar label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
        .board {
          display: flex;
          gap: 14px;
          overflow-x: auto;
        }
        .lane { margin-bottom: 18px; }
        .lane-title {
          font-weight: 600;
          margin: 0 0 8px 2px;
          color: var(--primary-text-color);
          font-size: 0.95em;
          cursor: pointer;
          user-select: none;
        }
        .lane-toggle {
          display: inline-block;
          width: 1em;
          font-size: 0.85em;
          opacity: 0.6;
        }
        .lane-count {
          font-weight: 400;
          opacity: 0.6;
          font-size: 0.9em;
        }
        .column {
          flex: 1 1 0;
          min-width: 300px;
          background: var(--card-background-color, #fff);
          border-radius: 8px;
          border: 1px solid var(--divider-color, #e0e0e0);
          display: flex;
          flex-direction: column;
        }
        .column-header {
          padding: 8px 10px;
          font-weight: 600;
          border-bottom: 1px solid var(--divider-color, #e0e0e0);
          display: flex;
          justify-content: space-between;
          color: var(--primary-text-color);
        }
        .column-body {
          padding: 6px;
          min-height: 50px;
          flex: 1;
        }
        .column-body.dragover {
          background: var(--primary-color, #03a9f4);
          opacity: 0.12;
        }
        .card-item {
          background: var(--ha-card-background, var(--secondary-background-color, #f5f5f5));
          border-radius: 6px;
          padding: 8px 10px;
          margin-bottom: 6px;
          cursor: grab;
          font-size: 0.9em;
          color: var(--primary-text-color);
          border-left: 3px solid var(--primary-color, #03a9f4);
        }
        .card-item:active { cursor: grabbing; }
        .card-key {
          font-weight: 600;
          font-size: 0.85em;
          opacity: 0.7;
          display: flex;
          align-items: center;
          gap: 4px;
        }
        .card-priority {
          width: 14px;
          height: 14px;
          flex: 0 0 auto;
        }
        .card-text {
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 8px;
        }
        .card-due {
          flex: 0 0 auto;
          font-size: 0.8em;
          opacity: 0.65;
          white-space: nowrap;
        }
        .card-due.overdue {
          opacity: 1;
          color: var(--error-color, #db4437);
          font-weight: 600;
        }
        .add-item {
          display: flex;
          gap: 4px;
          padding: 6px;
          border-top: 1px solid var(--divider-color, #e0e0e0);
        }
        .add-item input {
          flex: 1;
          border: 1px solid var(--divider-color, #e0e0e0);
          border-radius: 4px;
          padding: 5px 7px;
          font-size: 0.85em;
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color);
        }
        .add-item button {
          border: none;
          border-radius: 4px;
          background: var(--primary-color, #03a9f4);
          color: var(--text-primary-color, #fff);
          padding: 0 10px;
          cursor: pointer;
          font-size: 1.1em;
          line-height: 1;
        }
        select.project-filter {
          border: 1px solid var(--divider-color, #e0e0e0);
          border-radius: 4px;
          padding: 3px 6px;
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color);
        }
        input.search-filter {
          border: 1px solid var(--divider-color, #e0e0e0);
          border-radius: 4px;
          padding: 3px 6px;
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color);
          font-size: 0.9em;
          min-width: 140px;
        }
        .modal-backdrop {
          position: fixed;
          inset: 0;
          background: rgba(0, 0, 0, 0.5);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 1000;
        }
        .modal-backdrop[hidden] { display: none; }
        .modal {
          position: relative;
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color);
          border-radius: 8px;
          padding: 20px;
          max-width: 560px;
          width: 90%;
          max-height: 85vh;
          overflow-y: auto;
          box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
        }
        .modal-close {
          position: absolute;
          top: 8px;
          right: 8px;
          border: none;
          background: transparent;
          color: var(--primary-text-color);
          font-size: 1.2em;
          cursor: pointer;
          line-height: 1;
          padding: 6px;
          opacity: 0.6;
        }
        .modal-close:hover { opacity: 1; }
        .modal h2 { margin: 2px 26px 4px 0; font-size: 1.15em; }
        .modal-key { font-weight: 600; opacity: 0.65; font-size: 0.85em; }
        .modal-meta {
          display: flex;
          flex-wrap: wrap;
          gap: 8px 20px;
          margin: 14px 0;
          font-size: 0.88em;
        }
        .modal-meta div { display: flex; flex-direction: column; gap: 1px; }
        .modal-meta span.label { opacity: 0.6; font-size: 0.82em; }
        .modal-labels span {
          display: inline-block;
          background: var(--secondary-background-color, #eee);
          border-radius: 4px;
          padding: 2px 7px;
          margin: 0 4px 4px 0;
          font-size: 0.8em;
        }
        .modal-description {
          border-top: 1px solid var(--divider-color, #e0e0e0);
          margin-top: 12px;
          padding-top: 10px;
          font-size: 0.9em;
          line-height: 1.45;
          word-wrap: break-word;
        }
        .modal-description :first-child { margin-top: 0; }
        .modal-description :last-child { margin-bottom: 0; }
        .modal-link {
          display: inline-block;
          margin-top: 16px;
          font-size: 0.85em;
          color: var(--primary-color, #03a9f4);
        }
        .modal-error { color: var(--error-color, #db4437); }
      </style>
      <ha-card>
        <div class="toolbar">
          <label>
            <input type="checkbox" class="group-toggle" ${this._groupByEpic ? "checked" : ""} />
            Gruppieren nach Epic
          </label>
          <label>
            Projekt:
            <select class="project-filter"><option value="__all__">Alle</option></select>
          </label>
          <input type="text" class="search-filter" placeholder="Suchen …" />
        </div>
        <div class="board"></div>
      </ha-card>
      <div class="modal-backdrop" hidden>
        <div class="modal">
          <button class="modal-close" title="Schließen" aria-label="Schließen">✕</button>
          <div class="modal-body"><em>Lade …</em></div>
        </div>
      </div>
    `;
    root.querySelector(".group-toggle").addEventListener("change", (e) => {
      this._groupByEpic = e.target.checked;
      this._savePersisted();
      this._renderBoard();
    });
    this._projectSelectEl = root.querySelector(".project-filter");
    this._projectSelectEl.addEventListener("change", (e) => {
      this._projectFilter = e.target.value;
      this._savePersisted();
      this._renderBoard();
    });
    // Live/dynamic: filters as you type, no debounce - _renderBoard() only
    // touches already-fetched _itemsByEntity, no network round-trip, so
    // there's nothing worth debouncing against.
    root.querySelector(".search-filter").addEventListener("input", (e) => {
      this._searchQuery = e.target.value.trim().toLowerCase();
      this._renderBoard();
    });
    this._boardEl = root.querySelector(".board");

    this._modalBackdropEl = root.querySelector(".modal-backdrop");
    this._modalBodyEl = root.querySelector(".modal-body");
    root.querySelector(".modal-close").addEventListener("click", () => this._closeModal());
    this._modalBackdropEl.addEventListener("click", (e) => {
      if (e.target === this._modalBackdropEl) this._closeModal(); // outside the modal box itself
    });
    // A single document-level listener per card instance, not per-render -
    // _render() only runs once per config in practice, but guard anyway so
    // a hypothetical extra call can't stack up duplicate handlers.
    if (!this._escKeyHandler) {
      this._escKeyHandler = (e) => {
        if (e.key === "Escape" && this._modalBackdropEl && !this._modalBackdropEl.hidden) {
          this._closeModal();
        }
      };
      document.addEventListener("keydown", this._escKeyHandler);
    }

    this._renderBoard();
  }

  // ---- data fetching -------------------------------------------------

  // A `callWS` promise that never settles (dropped connection mid-flight,
  // browser tab throttled in the background, etc.) would otherwise hang
  // `_updateItems()` forever, leaving `_fetching` stuck `true` - every
  // later real change then just queues into `_pendingRefetch` and waits
  // for a reset that never comes, i.e. "works once, then needs a reload".
  // Race each column's fetch against a timeout instead of trusting it to
  // always settle on its own; connectedCallback's poll interval is the
  // second, coarser layer of the same defense.
  _withTimeout(promise, ms) {
    return Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), ms)),
    ]);
  }

  async _updateItems() {
    if (!this._hass || !this._config) return;
    this._fetching = true;
    try {
      for (const col of this._config.columns) {
        try {
          const resp = await this._withTimeout(
            this._hass.callWS({
              type: "call_service",
              domain: "todo",
              service: "get_items",
              service_data: { entity_id: col.entity },
              return_response: true,
            }),
            10000
          );
          const items = resp?.response?.[col.entity]?.items || [];
          this._itemsByEntity[col.entity] = items.map((item) => this._parseItem(item));
        } catch (err) {
          this._itemsByEntity[col.entity] = [];
        }
      }
      this._updateProjectOptions();
      this._renderBoard();
    } finally {
      this._fetching = false;
      if (this._pendingRefetch) {
        this._pendingRefetch = false;
        this._updateItems();
      }
    }
  }

  // Ascending by issue number (e.g. "HA-9" before "HA-21"), then project
  // key as a tiebreaker for a stable, predictable order within a column.
  _sortByKey(items) {
    return [...items].sort((a, b) => {
      const [projA, numA] = a.key.split("-");
      const [projB, numB] = b.key.split("-");
      if (projA !== projB) return (projA || "").localeCompare(projB || "");
      return (parseInt(numA, 10) || 0) - (parseInt(numB, 10) || 0);
    });
  }

  _parseItem(item) {
    let meta = {};
    try {
      meta = JSON.parse(item.description || "{}");
    } catch (err) {
      meta = {};
    }
    const match = item.summary.match(/^(\S+-\d+)\s+(.*)$/s);
    return {
      uid: item.uid,
      summary: item.summary,
      key: match ? match[1] : "",
      text: match ? match[2] : item.summary,
      project: meta.project || null,
      epicKey: meta.epic_key || null,
      epicName: meta.epic_name || null,
      priority: meta.priority || null,
      priorityIcon: meta.priority_icon || null,
      dueDate: meta.due_date || null,
    };
  }

  // "YYYY-MM-DD" (Jira's duedate is a date, no time component) - compared
  // against local midnight so "due today" never shows as overdue.
  _isOverdue(dueDate) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return new Date(`${dueDate}T00:00:00`) < today;
  }

  _formatDueDate(dueDate) {
    const [y, m, d] = dueDate.split("-");
    return `${d}.${m}.`;
  }

  _allItems() {
    return this._config.columns.flatMap((col) => this._itemsByEntity[col.entity] || []);
  }

  _updateProjectOptions() {
    if (!this._projectSelectEl) return;
    // Union with epic projects too, same reasoning as _epicsPresent(): a
    // project whose only current content is an empty Epic would
    // otherwise never appear in the filter dropdown either.
    const fromItems = this._allItems().map((i) => i.project);
    const fromEpics = this._allEpics().map((e) => e.project);
    const projects = [...new Set([...fromItems, ...fromEpics].filter(Boolean))].sort();
    const current = this._projectFilter;
    this._projectSelectEl.innerHTML =
      `<option value="__all__">Alle</option>` +
      projects.map((p) => `<option value="${p}">${p}</option>`).join("");
    // Keep the selection even if it briefly isn't in the freshly-seen set
    // (e.g. a project with 0 open issues right now, or a config default
    // that just hasn't shown up in data yet).
    if (![...this._projectSelectEl.options].some((o) => o.value === current)) {
      const opt = document.createElement("option");
      opt.value = current;
      opt.textContent = current === "__all__" ? "Alle" : current;
      this._projectSelectEl.appendChild(opt);
    }
    this._projectSelectEl.value = current;
  }

  _filterByProject(items) {
    if (this._projectFilter === "__all__") return items;
    return items.filter((i) => i.project === this._projectFilter);
  }

  _matchesSearch(item) {
    if (!this._searchQuery) return true;
    return (
      item.key.toLowerCase().includes(this._searchQuery) ||
      item.text.toLowerCase().includes(this._searchQuery)
    );
  }

  // Project filter + search, combined - the one place both apply together.
  // Search intentionally isn't folded into _filterByProject itself: the
  // project dropdown's own option list (_updateProjectOptions -> _allItems)
  // must stay unaffected by what's currently typed into the search box.
  _filterItems(items) {
    return this._filterByProject(items).filter((i) => this._matchesSearch(i));
  }

  // ---- rendering -------------------------------------------------------

  _renderBoard() {
    if (!this._boardEl) return;
    this._boardEl.innerHTML = "";
    if (this._groupByEpic) {
      this._renderLanes();
    } else {
      this._boardEl.style.flexDirection = "row";
      const row = this._buildColumnsRow(this._config.columns, null);
      this._boardEl.appendChild(row);
    }
  }

  // All epics the coordinator knows about for the configured project(s),
  // *including* ones with zero cards currently on the board - see
  // todo.py's extra_state_attributes. Read off whichever configured
  // column happens to be first; every column carries the same list.
  _allEpics() {
    const first = this._config.columns[0];
    return this._hass?.states[first.entity]?.attributes?.all_epics || [];
  }

  _epicsPresent() {
    const epics = new Map(); // key -> name
    for (const col of this._config.columns) {
      const items = this._filterItems(this._itemsByEntity[col.entity] || []);
      for (const item of items) {
        const key = item.epicKey || NO_EPIC;
        if (!epics.has(key)) epics.set(key, item.epicName || "Kein Epic");
      }
    }
    // Merge in empty Epics (no cards on the board at all right now) so
    // they still get a lane, same project filter as everything else - but
    // only when there's no active search: an Epic with zero cards can
    // never contain a search hit, so it should disappear like everything
    // else that doesn't match while searching, not get a free pass.
    if (!this._searchQuery) {
      for (const epic of this._allEpics()) {
        if (this._projectFilter !== "__all__" && epic.project !== this._projectFilter) continue;
        if (!epics.has(epic.key)) epics.set(epic.key, epic.name);
      }
    }
    // Real epics first (sorted by key), "Kein Epic" always last.
    const keys = [...epics.keys()].filter((k) => k !== NO_EPIC).sort();
    if (epics.has(NO_EPIC)) keys.push(NO_EPIC);
    return keys.map((k) => [k, epics.get(k)]);
  }

  _renderLanes() {
    this._boardEl.style.flexDirection = "column";
    const epics = this._epicsPresent();
    if (epics.length === 0) {
      this._boardEl.innerHTML = "<em>Keine Karten</em>";
      return;
    }
    for (const [epicKey, epicName] of epics) {
      const lane = document.createElement("div");
      lane.className = "lane";
      const collapsed = this._collapsedLanes.has(epicKey);
      const title = document.createElement("div");
      title.className = "lane-title";
      const label = epicKey === NO_EPIC ? "Kein Epic" : `${epicKey}  ${epicName}`;
      title.innerHTML =
        `<span class="lane-toggle">${collapsed ? "▶" : "▼"}</span>` +
        `${this._escapeHtml(label)} ` +
        `<span class="lane-count">(${this._laneCardCount(epicKey)})</span>`;
      // The whole title row toggles, not just the little arrow - a bigger,
      // more forgiving click target, and there's nothing else on it to
      // conflict with (unlike a card, there's no drag to guard against).
      title.addEventListener("click", () => {
        if (collapsed) this._collapsedLanes.delete(epicKey);
        else this._collapsedLanes.add(epicKey);
        this._savePersisted();
        this._renderBoard();
      });
      lane.appendChild(title);
      if (!collapsed) {
        lane.appendChild(this._buildColumnsRow(this._config.columns, epicKey));
      }
      this._boardEl.appendChild(lane);
    }
  }

  // Card count for a lane's header, respecting the current project filter
  // and search - so a collapsed lane still tells you at a glance whether
  // it's worth expanding under the active filter, and the count doesn't
  // lie relative to what expanding it would actually show.
  _laneCardCount(epicKey) {
    let count = 0;
    for (const col of this._config.columns) {
      const items = this._filterItems(this._itemsByEntity[col.entity] || []);
      count += items.filter((i) => (i.epicKey || NO_EPIC) === epicKey).length;
    }
    return count;
  }

  _buildColumnsRow(columns, epicFilter) {
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.gap = "14px";
    for (const col of columns) {
      let items = this._filterItems(this._itemsByEntity[col.entity] || []);
      if (epicFilter !== null) {
        items = items.filter((i) => (i.epicKey || NO_EPIC) === epicFilter);
      }
      row.appendChild(this._buildColumn(col, items, epicFilter));
    }
    return row;
  }

  _buildColumn(col, items, epicFilter) {
    const columnEl = document.createElement("div");
    columnEl.className = "column";
    columnEl.innerHTML = `
      <div class="column-header">
        <span>${col.title || col.entity}</span>
        <span class="count">${items.length}</span>
      </div>
      <div class="column-body"></div>
      <div class="add-item">
        <input type="text" placeholder="+ Aufgabe hinzufügen …" />
        <button title="Hinzufügen">+</button>
      </div>
    `;
    const body = columnEl.querySelector(".column-body");
    const addInput = columnEl.querySelector(".add-item input");
    const addButton = columnEl.querySelector(".add-item button");
    const submitNewItem = () => {
      const text = addInput.value.trim();
      if (!text || !this._hass) return;
      // Only pass a project override if a single, specific project is
      // selected - "Alle" gives no useful hint, backend then falls back to
      // its own configured default_project. Same idea for epic: only a
      // real Epic lane (not "Kein Epic", not the ungrouped view) counts
      // as a hint - typing a card there shouldn't invent a link.
      //
      // An Epic lane always wins the project too, even over an explicitly
      // selected project filter: an Epic only ever exists in one project
      // (its key's own prefix), and Jira flatly rejects creating an issue
      // whose project doesn't match its parent Epic's - so typing a card
      // into e.g. the "Sina" (FAM-2) lane while the filter happens to be
      // set to a different project used to silently fail every time.
      const payload = {};
      if (epicFilter && epicFilter !== NO_EPIC) {
        payload.epic = epicFilter;
        payload.project = epicFilter.split("-")[0];
      } else if (this._projectFilter !== "__all__") {
        payload.project = this._projectFilter;
      }
      const data = { entity_id: col.entity, item: text };
      if (Object.keys(payload).length > 0) data.description = JSON.stringify(payload);
      this._hass.callService("todo", "add_item", data);
      addInput.value = "";
    };
    addButton.addEventListener("click", submitNewItem);
    addInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitNewItem();
    });
    body.addEventListener("dragover", (e) => {
      e.preventDefault();
      body.classList.add("dragover");
    });
    body.addEventListener("dragleave", () => body.classList.remove("dragover"));
    body.addEventListener("drop", (e) => {
      e.preventDefault();
      body.classList.remove("dragover");
      this._onDrop(col.entity);
    });
    for (const item of this._sortByKey(items)) {
      const el = document.createElement("div");
      el.className = "card-item";
      el.draggable = true;
      const priorityHtml = item.priorityIcon
        ? `<img class="card-priority" src="${this._escapeHtml(item.priorityIcon)}" ` +
          `alt="${this._escapeHtml(item.priority || "")}" title="${this._escapeHtml(item.priority || "")}" ` +
          `onerror="this.remove()" />`
        : "";
      const dueHtml = item.dueDate
        ? `<span class="card-due${this._isOverdue(item.dueDate) ? " overdue" : ""}">` +
          `${this._formatDueDate(item.dueDate)}</span>`
        : "";
      el.innerHTML = item.key
        ? `<div class="card-key">${priorityHtml}${item.key}</div>` +
          `<div class="card-text">${item.text}${dueHtml}</div>`
        : `<div>${item.text}</div>`;
      // A completed native HTML5 drag normally doesn't also fire a `click`
      // on the source element, but that's an observed browser behavior,
      // not a spec guarantee - track it explicitly so an edge case can't
      // pop the details modal right after a drop. A genuine click (press +
      // release with no real movement) never fires `dragstart` at all, so
      // it's unaffected. `dragend` is deferred a tick since some browsers
      // appear to fire a trailing `click` right after it.
      let justDragged = false;
      el.addEventListener("dragstart", () => {
        this._dragItem = item;
        this._dragSourceEntity = col.entity;
        justDragged = true;
      });
      el.addEventListener("dragend", () => {
        setTimeout(() => {
          justDragged = false;
        }, 0);
      });
      el.addEventListener("click", () => {
        if (justDragged || !item.key) return;
        this._openDetails(item, col.entity);
      });
      body.appendChild(el);
    }
    return columnEl;
  }

  // ---- details popup ---------------------------------------------------

  _escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str ?? "";
    return div.innerHTML;
  }

  async _openDetails(item, entityId) {
    if (!this._hass || !this._modalBackdropEl) return;
    this._modalBackdropEl.hidden = false;
    this._modalBodyEl.innerHTML = "<em>Lade …</em>";
    try {
      const resp = await this._hass.callWS({
        type: "call_service",
        domain: "jira_board",
        service: "get_issue",
        service_data: { issue_key: item.key, board_entity_id: entityId },
        return_response: true,
      });
      // Guard against the modal having been closed (or reopened for a
      // *different* card) while this fetch was still in flight.
      if (this._modalBackdropEl.hidden) return;
      const data = resp?.response;
      if (!data) throw new Error("empty response");
      this._renderModalContent(data);
    } catch (err) {
      if (this._modalBackdropEl.hidden) return;
      this._modalBodyEl.innerHTML =
        `<div class="modal-error">Details für ${this._escapeHtml(item.key)} ` +
        `konnten nicht geladen werden.</div>`;
    }
  }

  _closeModal() {
    if (this._modalBackdropEl) this._modalBackdropEl.hidden = true;
  }

  _renderModalContent(data) {
    const esc = (s) => this._escapeHtml(s);
    const person = (p) => (p?.name ? esc(p.name) : "–");
    const fmtDate = (iso) => {
      if (!iso) return "–";
      const d = new Date(iso);
      return Number.isNaN(d.getTime()) ? esc(iso) : esc(d.toLocaleString());
    };
    const fmtDueDate = (dateStr) => {
      if (!dateStr) return "–";
      const d = new Date(`${dateStr}T00:00:00`);
      return Number.isNaN(d.getTime()) ? esc(dateStr) : esc(d.toLocaleDateString());
    };
    const priorityHtml = data.priority_icon
      ? `<img class="card-priority" src="${esc(data.priority_icon)}" alt="" onerror="this.remove()" /> `
      : "";
    const labels = (data.labels || []).map((l) => `<span>${esc(l)}</span>`).join("");
    // description_html comes straight from Jira's own `renderedFields`
    // (expand=renderedFields on the get_issue call) - already-rendered
    // HTML from Jira's own renderer, inserted as-is rather than
    // re-escaped; everything else on this card is free-text/user input
    // and goes through esc() above.
    this._modalBodyEl.innerHTML = `
      <div class="modal-key">${esc(data.key)}${data.issue_type ? " · " + esc(data.issue_type) : ""}</div>
      <h2>${esc(data.summary)}</h2>
      <div class="modal-meta">
        <div><span class="label">Status</span>${esc(data.status) || "–"}</div>
        <div><span class="label">Projekt</span>${esc(data.project) || "–"}</div>
        <div><span class="label">Priorität</span>${priorityHtml}${esc(data.priority) || "–"}</div>
        <div><span class="label">Assignee</span>${person(data.assignee)}</div>
        <div><span class="label">Reporter</span>${person(data.reporter)}</div>
        <div><span class="label">Fällig</span>${fmtDueDate(data.due_date)}</div>
        <div><span class="label">Erstellt</span>${fmtDate(data.created)}</div>
        <div><span class="label">Aktualisiert</span>${fmtDate(data.updated)}</div>
      </div>
      ${labels ? `<div class="modal-labels">${labels}</div>` : ""}
      <div class="modal-description">${data.description_html || "<em>Keine Beschreibung</em>"}</div>
      <a class="modal-link" href="${esc(data.url)}" target="_blank" rel="noopener noreferrer">In Jira öffnen ↗</a>
    `;
  }

  _onDrop(targetEntity) {
    const item = this._dragItem;
    const sourceEntity = this._dragSourceEntity;
    if (!item || !this._hass) return;
    if (sourceEntity === targetEntity) return; // dropped back where it was

    // Optimistic update: move the card in local state and repaint
    // immediately, *then* fire the sync in the background. The real
    // Jira round-trip (transition lookup + transition + coordinator
    // refresh) takes a couple of seconds; there's no reason to make the
    // person dragging the card sit and watch that happen. If the move
    // ends up rejected (workflow doesn't allow it) or something else goes
    // wrong, the next real refresh corrects the board automatically - see
    // todo.py's `note_local_move` / `just_moved` on the backend side,
    // which is exactly the same kind of "assume it worked, reconcile on
    // the next poll" mechanism, just on the other end of the sync.
    const list = this._itemsByEntity[sourceEntity] || [];
    const idx = list.findIndex((i) => i.uid === item.uid);
    if (idx !== -1) list.splice(idx, 1);
    (this._itemsByEntity[targetEntity] ||= []).push(item);
    this._renderBoard();

    this._hass.callService("todo", "remove_item", {
      entity_id: sourceEntity,
      item: item.uid,
    });
    this._hass.callService("todo", "add_item", {
      entity_id: targetEntity,
      item: item.summary,
    });
    this._dragItem = null;
    this._dragSourceEntity = null;
  }
}

// Registered via both add_extra_js_url (auto-loads with zero setup) *and*
// as an explicit Lovelace resource (the mechanism every other bundled card
// in this instance uses, and apparently the one the Companion App's
// WebView actually needs - add_extra_js_url alone rendered as a config
// error there). Both loading this same script is possible, so guard
// against a duplicate customElements.define(), which throws and would
// otherwise silently break the rest of this file's execution.
if (!customElements.get("jira-board-card")) {
  customElements.define("jira-board-card", JiraBoardCard);

  // Make it show up in the "Add Card" picker.
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "jira-board-card",
    name: "Jira Board",
    description: "Drag-and-drop Kanban-Board für jira_board todo-Spalten, optional gruppiert nach Epic",
  });
}
