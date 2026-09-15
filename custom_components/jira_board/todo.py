"""Todo platform for Jira Board - one column (Jira status) per todo.* entity.

Design note on move vs. delete semantics: a Kanban-style drag in a frontend
like Hakanban is implemented as "remove item from source list, add it to
target list" - i.e. a delete call on one entity followed by a create call on
another, with no reliable signal tying the two together or fixing their
order. Rather than guess, `async_create_todo_item` is treated as the sole
authoritative "this issue is now in this column" signal (it transitions the
Jira issue, or creates a new one if the key is unseen), and
`async_delete_todo_items` is intentionally a no-op against Jira. Making
delete destructive (e.g. auto-transitioning to Done) risks silently closing
an issue that was merely being repositioned. The coordinator's next refresh
is always the final source of truth, so a delete with no matching create
elsewhere just has the card reappear rather than something being lost.

Design note on identifying moves: HA's standard `todo` create path
(`todo.add_item`, and every frontend built on it, confirmed against
Hakanban's own list/card model) does not let the caller choose `uid` - the
entity always assigns it. So a drag-move can't be recognized by uid staying
stable across the delete+create pair; it never does. Every card's summary
is written out as "KEY  text" (see `todo_items` below) specifically so the
Jira key can instead be recovered from the *summary text*, which - being
the visible card content - is what actually survives a drag unchanged.
"""
from __future__ import annotations

import json
import logging
import re

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import JiraApiError
from .const import COLUMNS, COLUMN_SLUGS, DOMAIN
from .coordinator import JiraBoardCoordinator

_LOGGER = logging.getLogger(__name__)

# Matches the "KEY  " prefix this integration writes at the start of every
# card summary, e.g. "HUG-38  Bidet" -> key="HUG-38".
_KEY_PREFIX_RE = re.compile(r"^([A-Z][A-Z0-9]*-\d+)\s+")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: JiraBoardCoordinator = hass.data[DOMAIN][entry.entry_id]
    # default_project is read from the coordinator (not entry.data directly)
    # since it may have been changed later via the options flow - see
    # __init__.py's data/options resolution.
    async_add_entities(
        JiraBoardColumn(coordinator, entry.entry_id, column) for column in COLUMNS
    )


class JiraBoardColumn(CoordinatorEntity[JiraBoardCoordinator], TodoListEntity):
    """One Kanban column (= one Jira status) as a todo list."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        # Lets a *new-card* create call pass a target project override in
        # `description` (see async_create_todo_item) - unrelated to how
        # `todo_items` below uses `description` for epic/project metadata
        # on reads, that's a separate direction and never collides since a
        # brand new card has no prior description of its own yet.
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(
        self,
        coordinator: JiraBoardCoordinator,
        entry_id: str,
        column: str,
    ) -> None:
        super().__init__(coordinator)
        self._column = column
        self._attr_unique_id = f"{entry_id}_{COLUMN_SLUGS[column]}"
        self._attr_name = column
        self._attr_icon = "mdi:card-multiple-outline"

    @property
    def extra_state_attributes(self) -> dict:
        # Rides along on every column entity (cheap, small list) so the
        # card can read it off whichever configured column it likes - see
        # jira-board-card.js's "Group by Epic" lanes for why this exists:
        # an Epic with zero cards currently on the board would otherwise
        # never get a lane, since todo_items only carries epic info for
        # issues that actually have a parent Epic.
        return {
            "all_epics": self.coordinator.all_epics,
            # Same reasoning as all_epics above - rides along so the
            # details popup's edit-mode priority dropdown (jira-board-
            # card.js) has real, site-configured options instead of
            # guessing at the standard Highest/High/Medium/Low/Lowest set,
            # which custom Jira workflows aren't guaranteed to use.
            "all_priorities": self.coordinator.all_priorities,
        }

    @property
    def todo_items(self) -> list[TodoItem]:
        issues = self.coordinator.data.get(self._column, [])
        done = self._column == "Done"
        return [
            TodoItem(
                uid=i["key"],
                summary=f"{i['key']}  {i['summary']}",
                status=TodoItemStatus.COMPLETED if done else TodoItemStatus.NEEDS_ACTION,
                # TodoItem has no dedicated field for this, so the frontend
                # card's extra metadata (project, epic, priority, due date)
                # rides along as a small JSON blob rather than a plain
                # string - see jira-board-card.js's "Group by Epic" lanes.
                description=json.dumps(
                    {
                        "project": i["project"],
                        "epic_key": i.get("epic_key"),
                        "epic_name": i.get("epic_name"),
                        "priority": i.get("priority"),
                        "priority_icon": i.get("priority_icon"),
                        "due_date": i.get("due_date"),
                    }
                ),
            )
            for i in issues
        ]

    def _known_columns_for(self, key: str) -> list[str]:
        """Which column(s) the coordinator currently thinks this key is in."""
        return [
            col
            for col, issues in self.coordinator.data.items()
            if any(i["key"] == key for i in issues)
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        client = self.coordinator.client
        summary = item.summary or "New task"
        match = _KEY_PREFIX_RE.match(summary)
        key = match.group(1) if match else None
        existing_columns = self._known_columns_for(key) if key else []

        if key and existing_columns and self._column not in existing_columns:
            # Known issue reappearing (by its "KEY  text" prefix) in a
            # different column -> this is a drag-move. Transition the real
            # Jira issue to match.
            moved = await client.transition_to_status(key, self._column)
            if not moved:
                _LOGGER.warning(
                    "Jira workflow rejected moving %s to '%s' - reverting on next refresh",
                    key,
                    self._column,
                )
                await self.coordinator.async_request_refresh()
                return
            self.coordinator.note_local_move(key, self._column)
            await self.coordinator.async_request_refresh()
            return

        if key and self._column in existing_columns:
            # Recreated in the *same* column it's already in (e.g. a
            # refresh artifact) - nothing to do.
            return

        # No recognizable "KEY  " prefix -> a genuinely new card was typed
        # directly on the board, create a real Jira issue for it.
        # `description` (only meaningful here, on creation - see the
        # supported_features comment above) carries the project filter
        # and, if the card was typed inside a specific Epic's lane, that
        # Epic too, so the new issue is linked from the start instead of
        # landing in "Kein Epic". Two shapes for backward compatibility:
        # a bare project-key string (older card versions / anything else
        # driving todo.add_item directly), or a JSON blob
        # `{"project": ..., "epic": ...}` (current card.js).
        target_project = self.coordinator.default_project
        epic_key = None
        if item.description:
            try:
                payload = json.loads(item.description)
                if not isinstance(payload, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                payload = {"project": item.description}
            if payload.get("project") in self.coordinator.projects:
                target_project = payload["project"]
            epic_key = payload.get("epic")
            if epic_key:
                # An Epic only ever exists in one project (its key's own
                # prefix, e.g. "FAM-2" -> "FAM") - Jira flatly rejects
                # creating an issue whose project doesn't match its
                # parent Epic's project ("must be created in the same
                # project as the parent"). Typing a card inside a
                # specific Epic's lane always means "this Epic's
                # project", regardless of what the board's project
                # filter happens to be set to - trust the Epic over
                # `payload["project"]`/default_project whenever they
                # disagree, rather than let the whole create fail
                # (found via the "Sina"/FAM-2 epic, 2026-09-15: the
                # project filter/default was on a different project,
                # so every card typed into that lane silently failed).
                epic_project = epic_key.split("-")[0]
                if epic_project in self.coordinator.projects:
                    target_project = epic_project
        try:
            new_key = await client.create_issue(target_project, summary, epic_key=epic_key)
        except JiraApiError as err:
            _LOGGER.error("Could not create Jira issue for '%s': %s", summary, err)
            return
        if self._column != COLUMNS[0]:
            await client.transition_to_status(new_key, self._column)
        self.coordinator.note_local_move(new_key, self._column)
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        # Only an explicit checkbox-complete is unambiguous enough to act
        # on; summary/description edits aren't synced back in this version.
        if item.uid and item.status == TodoItemStatus.COMPLETED:
            client = self.coordinator.client
            if await client.transition_to_status(item.uid, "Done"):
                self.coordinator.note_local_move(item.uid, "Done")
                await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        # Intentional no-op against Jira - see module docstring. Just
        # refresh so any card that's genuinely gone (deleted upstream)
        # drops out on its own.
        _LOGGER.debug(
            "Delete on column '%s' for %s - not transitioning Jira, see module docstring",
            self._column,
            uids,
        )
        await self.coordinator.async_request_refresh()
