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
        "jira-board-card: 'columns' ist erforderlich, z.B. " +
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

    this._dragItem = null;
    this._itemsByEntity = {};
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
        JSON.stringify({ groupByEpic: this._groupByEpic, projectFilter: this._projectFilter })
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
  }

  _render() {
    const root = this.attachShadow ? (this.shadowRoot || this.attachShadow({ mode: "open" })) : this;
    root.innerHTML = `
      <style>
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
        </div>
        <div class="board"></div>
      </ha-card>
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
    this._boardEl = root.querySelector(".board");
    this._renderBoard();
  }

  // ---- data fetching -------------------------------------------------

  async _updateItems() {
    if (!this._hass || !this._config) return;
    this._fetching = true;
    try {
      for (const col of this._config.columns) {
        try {
          const resp = await this._hass.callWS({
            type: "call_service",
            domain: "todo",
            service: "get_items",
            service_data: { entity_id: col.entity },
            return_response: true,
          });
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
    };
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
      const items = this._filterByProject(this._itemsByEntity[col.entity] || []);
      for (const item of items) {
        const key = item.epicKey || NO_EPIC;
        if (!epics.has(key)) epics.set(key, item.epicName || "Kein Epic");
      }
    }
    // Merge in empty Epics (no cards on the board at all right now) so
    // they still get a lane, same project filter as everything else.
    for (const epic of this._allEpics()) {
      if (this._projectFilter !== "__all__" && epic.project !== this._projectFilter) continue;
      if (!epics.has(epic.key)) epics.set(epic.key, epic.name);
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
      const title = document.createElement("div");
      title.className = "lane-title";
      title.textContent = epicKey === NO_EPIC ? "Kein Epic" : `${epicKey}  ${epicName}`;
      lane.appendChild(title);
      lane.appendChild(this._buildColumnsRow(this._config.columns, epicKey));
      this._boardEl.appendChild(lane);
    }
  }

  _buildColumnsRow(columns, epicFilter) {
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.gap = "14px";
    for (const col of columns) {
      let items = this._filterByProject(this._itemsByEntity[col.entity] || []);
      if (epicFilter !== null) {
        items = items.filter((i) => (i.epicKey || NO_EPIC) === epicFilter);
      }
      row.appendChild(this._buildColumn(col, items));
    }
    return row;
  }

  _buildColumn(col, items) {
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
      // its own configured default_project.
      const data = { entity_id: col.entity, item: text };
      if (this._projectFilter !== "__all__") data.description = this._projectFilter;
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
      el.innerHTML = item.key
        ? `<div class="card-key">${item.key}</div><div>${item.text}</div>`
        : `<div>${item.text}</div>`;
      el.addEventListener("dragstart", () => {
        this._dragItem = item;
        this._dragSourceEntity = col.entity;
      });
      body.appendChild(el);
    }
    return columnEl;
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
