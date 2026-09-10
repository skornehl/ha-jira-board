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

class JiraBoardCard extends HTMLElement {
  setConfig(config) {
    if (!config.columns || !Array.isArray(config.columns)) {
      throw new Error(
        "jira-board-card: 'columns' ist erforderlich, z.B. " +
          "[{entity: 'todo.to_do', title: 'To Do'}, ...]"
      );
    }
    this._config = config;
    this._dragUid = null;
    this._dragSummary = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._updateItems();
  }

  getCardSize() {
    return 6;
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
        .board {
          display: flex;
          gap: 12px;
          overflow-x: auto;
        }
        .column {
          flex: 1 1 0;
          min-width: 200px;
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
          min-height: 60px;
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
      </style>
      <ha-card>
        <div class="board"></div>
      </ha-card>
    `;
    this._boardEl = root.querySelector(".board");
    this._columnBodies = {};
    for (const col of this._config.columns) {
      const columnEl = document.createElement("div");
      columnEl.className = "column";
      columnEl.innerHTML = `
        <div class="column-header">
          <span>${col.title || col.entity}</span>
          <span class="count">0</span>
        </div>
        <div class="column-body"></div>
      `;
      const body = columnEl.querySelector(".column-body");
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
      this._columnBodies[col.entity] = { body, countEl: columnEl.querySelector(".count") };
      this._boardEl.appendChild(columnEl);
    }
  }

  _onDrop(targetEntity) {
    if (!this._dragUid || !this._hass) return;
    const sourceEntity = this._dragSourceEntity;
    if (sourceEntity === targetEntity) return; // dropped back where it was
    this._hass.callService("todo", "remove_item", {
      entity_id: sourceEntity,
      item: this._dragUid,
    });
    this._hass.callService("todo", "add_item", {
      entity_id: targetEntity,
      item: this._dragSummary,
    });
    this._dragUid = null;
    this._dragSummary = null;
    this._dragSourceEntity = null;
  }

  async _updateItems() {
    if (!this._hass || !this._columnBodies) return;
    for (const col of this._config.columns) {
      const stateObj = this._hass.states[col.entity];
      const { body, countEl } = this._columnBodies[col.entity];
      if (!stateObj) {
        body.innerHTML = "<em>nicht verfügbar</em>";
        continue;
      }
      countEl.textContent = stateObj.state;
      let items = [];
      try {
        const resp = await this._hass.callWS({
          type: "call_service",
          domain: "todo",
          service: "get_items",
          service_data: { entity_id: col.entity },
          return_response: true,
        });
        items = resp?.response?.[col.entity]?.items || [];
      } catch (err) {
        body.innerHTML = "<em>Fehler beim Laden</em>";
        continue;
      }
      body.innerHTML = "";
      for (const item of items) {
        const el = document.createElement("div");
        el.className = "card-item";
        el.draggable = true;
        const [, key, rest] = item.summary.match(/^(\S+-\d+)\s+(.*)$/s) || [null, "", item.summary];
        el.innerHTML = key
          ? `<div class="card-key">${key}</div><div>${rest}</div>`
          : `<div>${item.summary}</div>`;
        el.addEventListener("dragstart", () => {
          this._dragUid = item.uid;
          this._dragSummary = item.summary;
          this._dragSourceEntity = col.entity;
        });
        body.appendChild(el);
      }
    }
  }
}

customElements.define("jira-board-card", JiraBoardCard);

// Make it show up in the "Add Card" picker.
window.customCards = window.customCards || [];
window.customCards.push({
  type: "jira-board-card",
  name: "Jira Board",
  description: "Drag-and-drop Kanban-Board für jira_board todo-Spalten",
});
