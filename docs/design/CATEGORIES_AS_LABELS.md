# Categories are labels; access plans choose them

Status: decided 2026-11-03. Replaces the per-category
audience (added 2026-10-13 in 8e777d851; this change removes it) and supersedes the
unmerged standing-grant branches (`t3/repair-category-inventory-previews` and
related).

## Grounding (internal research notes)

- The access-plan research
  recommends "keep category restrictions inside plans with categories managed as
  reusable catalog data", one plan per person and one per app, intersected.
- Same entry [PX1, PX3]: Plex invites select libraries; "All Libraries" is a dynamic
  grant that includes libraries added later; the family example selects libraries one
  by one to avoid that. [JF2]: Jellyfin "Enable access to all libraries"; turning it
  off withholds future libraries.
- Same entry [report-ap]: edits to a shared plan reach existing assignments, so show
  who gains or loses before saving.

The 10-13 per-category audience put the rule on the label, against that entry. That
is the root of the 10-21 and 10-30 confusion: access was set in two places (the plan
and each category), and a plan named "Everything" did not include the categories.

## The model

1. **A category is a label.** It has an id, a name, a one-line description and its
   members (models and workflows). It holds no audience. The bundled presets (Adult,
   Experimental, Legacy) stay inert data until the owner adds them.
2. **An access plan chooses categories.** The plan body gets one field:
   `categories: "all" | [category ids]`.
   - `"all"`: every category, including ones added later (Plex "All Libraries").
   - a list: only these. A category added later is not included.
   - A plan with no field, and every new plan, starts as `[]` (no categories).
   - Ids are compared exactly as written. The gateway does not trim them, and it
     refuses an empty id, and a list that names one id twice.
3. **Rule.** An item that is in no category is governed by the plan's other settings
   only, as today. An item in one or more categories is usable only when the plan
   includes every one of those categories (deny wins).
4. **Person and app.** Unchanged: a request needs both its person's plan and its
   app's plan to include the item's categories. The owner using their own personal
   credentials sees everything (unchanged `is_owner_person` exception).
5. **One evaluator.** Runtime enforcement, every listing (`/v1/models`, the general
   capability list, Home, MCP), the plan summaries and the category reach preview call
   the same function.
6. **Names grant nothing.** The fresh-install full plan stays "All capabilities" with
   `categories: []`; its summary says which categories it includes.

## UI

- **People > Access plans, plan editor:** a "Categories" section with two choices:
  "All categories, including ones added later" and "Only these" with one checkbox per
  category (name and description). When the categories change, Save waits for a
  who-is-reached check of the current selection. The check lists each item that a
  person and an app can now use or can no longer use, with the category that moves it,
  so one category with two items shows two lines. If the check fails, or the gateway
  cannot tell who is reached, Save stays off until the owner chooses "Save anyway".
  Plan summaries say "All categories" / "Adult, Legacy" / "No categories".
- **Categories page:** no audience editor. Each category shows "Who can use it: you,
  and plans that include it: Everything (Mr, open-webui)", each plan linking to its
  editor, and a line that a plan with "All categories" includes it. Creating a
  category says it is hidden from everyone but you until a plan includes it.
- The category reach preview added in r123/r124 (labelling an item changes who can
  use it) stays and uses the new evaluator. It decides each item for each person-and-app
  pair, so moving one item into or out of a category is a gain or a loss for that item
  only, not for the whole category.
- The owner can change their own plan in People (the owner reported they could not on 10-30;
  verify and fix if still true).

## Migration (admin store schema bump, runs once at startup)

For every plan: `categories` = the ids of active categories whose old audience was
None (everyone) or listed this plan. A category whose old audience was an empty list
(owner only) goes to no plan. This preserves every current decision exactly; it never
converts a list to `"all"`. New category snapshots carry no audience; the snapshot decoder
ignores the old key in stored history. Restoring an old revision restores labels and
members, not audiences.

## Out of scope

- The Advanced rules (snapshot plans, people, apps, services) are unchanged.
- No cross-store activation binding: a plan save is one admin-store transaction, and
  category activation keeps its r124 review.
- Live data is the lead's job after deploy: give the owner's "Everything" and "Everything
  and All Categories" plans `"all"` (the owner's 10-30 intent: SNOFS in Open WebUI).
