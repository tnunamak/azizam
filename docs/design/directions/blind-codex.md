# Two visual directions for Azizam

Independent proposal, 2026-10-07. Read PRODUCT, PRINCIPLES, VOICE, taste ledger and ban list in that order, then the board. Opened all 28 referenced public images, all six anti-reference images, and all seven owner handoff PNGs; read the handoff README after the images. No other direction or outside research was read.

These are operating interfaces. Home answers what a person can do next; Status answers what is affected and where to act; Library answers where their creation went. The names below describe the interface, not a brand concept. All wireframe names, states, dates and creations are hypothetical layout examples, not claims about the running gateway.

## Rules shared by both directions

- The member shell is the base. Desktop navigation starts with Home, Apps & keys, Try and Library; an Admin disclosure adds Status, Engines, Providers, People, Usage, Activity and Settings. Members do not need engine inventory to connect an app. A phone has Home, Apps & keys, Library and More in a labelled bottom bar; More contains Try and the permitted owner tools. This proposal keeps detailed Status with owner tools pending the PRODUCT decision about member access.
- One 56 px phone header, 44 px minimum controls, at least 48 px action rows, 16 px body text. At 390 px there is no sideways page scroll. Bottom navigation reserves its own content clearance plus the device safe area; full-page captures make it static.
- One underline-tab pattern throughout, only for mutually exclusive views. Filters are labelled selects in one toolbar, never a second row of chips. No caps eyebrows, decorative metadata strings, ornamental numbering, italic headings, pill buttons, status left edges or ubiquitous arrow suffixes.
- A configured app is not proved working. Show a successful result only after an explicit successful check or receipt. Ready capability claims require current evidence for that person's access; disabled actions explain the restriction beside the action. Stale evidence includes its last successful check time; an untested capability says “We haven't checked yet.”
- State always has words and a distinct icon: check for OK, triangle for attention, cross for failed, question mark for unknown. Colour helps scanning and never supplies the only meaning. An incident identifies the evidenced layer and one action; an unidentified cause says “We couldn't tell where this stopped.”
- Every chart names a period and has records behind it. Missing samples are gaps, not zeroes; missing cost reads “Cost not recorded.” No decorative sparkline slots or fabricated uptime. Healthy verdicts require scope and evidence; an unknown scope never borrows a green global verdict.
- One fact occupies one place. A Status incident is excluded from the resolved-history list; Home does not repeat an endpoint below setup; Library detail owns its title and receipt facts rather than repeating them across tiles and a panel.

For either Status layout, a healthy state replaces the incident with one scoped verdict, such as “Enabled capabilities are working,” only after successful current checks. Unknown coverage leads with “We can't confirm Voice yet” and one check action, while confirmed capabilities remain individually readable. Multiple incidents are ordered by member impact, with one expanded at a time; disabled capabilities are labelled Paused rather than counted as failures. These are state rules, not new product features assumed to exist.

## Direction 1: Console

**Default:** dark. **Background:** `#102A2B`. **Accent:** `#99BFD5`. **Display:** IBM Plex Sans Condensed. **UI:** IBM Plex Sans. **Numerals:** IBM Plex Sans with tabular figures. **Copied identifiers/code:** IBM Plex Mono. **Rationale:** a compact, green-blue console gives an owner readable evidence and gives a member direct actions, with the approved deep-water art confined to one place.

**Real-world referent:** a ship's bridge alarm annunciator: named systems in fixed positions, one conspicuous active condition, aligned readings and quiet healthy channels. Borrow its scanning discipline, not maritime vocabulary, radar graphics, dials or simulated instruments. Household Home still speaks about apps and creations.

### References and the traits taken

| Board ID | Specific trait taken from the opened image | Application |
| --- | --- | --- |
| R14 | Compact sidebar and top rail; app table and activity have different widths and densities | A stable desktop frame with useful depth below the main task, without four identical KPI cards |
| R16 | Setup becomes a vertical sequence above app/activity regions and bottom navigation | Phone setup reveals only the next incomplete step; the action remains above the fold |
| R17 | The fin artwork is an unmistakable focal point against a deep field | A small cropped Azizam artwork area on Home; no slogan and no glow behind controls |
| R18 | Incident precedes a service table, with a separate history region | Scoped verdict before the table; resolved history stays distinct from the current incident |
| R04 | History strips share a baseline and time range; names and current states align | One shared capability history matrix, rather than unrelated sparklines in every row |
| R05 | Named infrastructure groups disclose their children | Gateway, engines, providers and workflows can be investigated without exposing the entire registry at once |
| R09 | Quiet properties occupy a narrow region beside the working content | Receipt facts stay subordinate to the outcome and creation |
| R20 | The selected object remains visible while its context panel explains it | Library selection preserves the working list; detail becomes a full phone view |
| R12 | The actual image/video dominates the viewer; filmstrip and transport sit at its edge | Library concentrates visual emphasis in one selected creation, not a row of decorated shells |

### Palette and measured contrast

Exactly six UI colours per variant. The accent doubles as the unknown-state colour, distinguished by its question-mark icon and written state; it never means healthy. All panels use the listed background, so the following foreground ratios apply to both the page and panel interiors. Artwork may contain other colours, but labels never sit directly over it.

| Role | Light hex | Contrast on `#F3F7F6` | Dark hex | Contrast on `#102A2B` |
| --- | --- | ---: | --- | ---: |
| Background | `#F3F7F6` | — | `#102A2B` | — |
| Text | `#173332` | 12.50:1 | `#F2F7F6` | 13.98:1 |
| Accent / unknown | `#315E80` | 6.39:1 | `#99BFD5` | 7.76:1 |
| OK | `#246447` | 6.51:1 | `#8ECBB0` | 8.15:1 |
| Attention | `#815115` | 6.23:1 | `#E1B975` | 8.21:1 |
| Failed | `#A03932` | 6.22:1 | `#F09B93` | 7.07:1 |

Computed with WCAG sRGB relative luminance: linearize each RGB channel at 0.04045, apply 0.2126/0.7152/0.0722 weights, then `(lighter + 0.05) / (darker + 0.05)`. Values are rounded to two decimals; every text/state pair exceeds normal-text AA's 4.5:1. Primary controls use the accent as fill and the variant background as their text: 6.39:1 light, 7.76:1 dark. State text is fully opaque on background, with no tinted status fills. The light variant is cool green-white, with the same dense alignment and sans typography; it is not a cream-and-serif conversion.

### Type and construction

Sizes are CSS pixels, shown as size/line-height. Only the verdict or main page title can use display type; prose never uses the condensed face.

| Role | Face | Desktop | Phone | Weight | Tracking |
| --- | --- | --- | --- | ---: | --- |
| Display / scoped verdict | IBM Plex Sans Condensed | 40/44 | 30/34 | 600 | -0.015em |
| h1 / page title | IBM Plex Sans | 28/34 | 26/32 | 600 | -0.01em |
| h2 / region title | IBM Plex Sans | 20/26 | 20/26 | 600 | 0 |
| Body / controls | IBM Plex Sans | 16/24 | 16/24 | 400; controls 500 | 0 |
| Small / table labels | IBM Plex Sans | 14/20 | 14/20 | 400; labels 500 | 0 |
| Numeral / telemetry | IBM Plex Sans, tabular | 24/28; tables 16/24 | 20/26; rows 16/24 | 500 | 0 |
| Exact model name, URL, request ID, code | IBM Plex Mono | 14/20 | 14/20 | 400 | 0 |

Sentences such as “No paid requests” use body type, never the numeral role. A large reading retains its unit and period beside it. Font fallback is sans-serif for reading and monospace only for exact strings; actual font coverage and loading need validation during implementation.

- **Spacing:** 4, 8, 12, 16, 24, 32, 48 px. Phone gutters 16; desktop content gutters 24. Desktop sidebar 208, top rail 56. Table rows 48; cells have 12 px vertical and 16 px horizontal padding. The four-track desktop history matrix is 128 px high including title and legend; it is a passive figure, with dated records accessible through the table action. Dense readings sit within rows, not separate metric tiles.
- **Radius:** controls 4 px; enclosing task/incident regions 8 px; media viewport 0 px inside its 8 px enclosing viewer. Nested rows have no individual rounded backgrounds. No fully rounded primary actions.
- **Elevation:** border only; no shadows or glow. Structural regions have 1 px text-colour borders at 24% opacity; row separators appear only where alignment needs them, at 16%. Interactive outlines and focus use the full accent colour, with a 2 px focus outline and 2 px gap. Low-contrast structural separators never carry the identity of a control or a state.
- **Icons:** one 20 px outline family, 1.75 px stroke, squared rather than bubbly shapes; 24 px in phone navigation. Home is a house, Apps & keys a key, Library a framed image, Status a shield, Usage a currency symbol, Activity a list. Distinct silhouettes, visible destination labels, no coloured icon tiles in every row. Status icons have the shapes specified above.
- **Theme behaviour:** the setting changes colours, not hierarchy, row heights or state meanings. No translucent glass panels. Dark atmosphere comes from the green-blue field, deliberate contrast and bounded artwork rather than blue-violet gradient chrome.

### First viewport and the one bold element

| Surface | What the reader learns in two seconds | Single memorable element | What stays quiet |
| --- | --- | --- | --- |
| Home, 390 px | Chat is usable, and they can open it or connect an app without learning gateway architecture | One 80 px-high crop of the approved fin-art grammar inside the opening task region; maximum 15% of an 844 px viewport | A short readiness sentence and rectangular actions; app rows and recent work have ordinary type, no KPI band |
| Status, 1440 px | Which capability is affected, the evidenced failure layer, and the next action | One shared, 7-day capability history matrix: aligned rows make failures and gaps a recognizable visual pattern | Verdict comes first; eight or more named operational rows, textual state and evidence age; one subordinate resolved-history column |
| Library, 1440 px | Their most recently selected creation is here, and its file and receipt can be opened | One large creation viewport occupying at least 65% of its viewer region, with visible playback controls for timed media | An indexed thumbnail list at left and a slim file/receipt region at right; no branded illustration competing with the creation |

Home artwork is decorative, never a green/red health illustration. Status replaces the matrix with “No history saved” and a compact table when records are absent; its bold element then becomes the scoped verdict, not a fake graph. Library without a selected creation uses a plain selection prompt; an empty Library uses one compact explanation and a Try action, without a fake media grid. Each surface has at most one such focal element at a time.

### Wireframes

ASCII is structural, not a font or pixel simulation. Brackets indicate functioning controls; state words are passive text. Home assumes completed setup and current evidence for Chat. First-time Home replaces the app list with one next setup step, rather than adding a second hero.

**Home: 390 × 844 px; 16 px gutters, 358 px content.**

```text
+--------------------------------------+
| Azizam                       [Account]| 56
+--------------------------------------+
| Home                                 |
| +----------------------------------+ |
| |   one fin artwork crop, 80 high   | |
| | Chat is ready.                   | |
| | [Open chat]                      | | 48 high
| | [Connect an app]                 | | 48 high
| +----------------------------------+ |
|                                      |
| Your apps                 [View all] |
| Daisy               Last used today  | 48 high
| LibreChat           Last used Monday | 48 high
|                                      |
| Recent creation                      |
| [small actual thumbnail] [Open]       |
|                                      |
| Images are paused.                   |
| Ask Sam to turn them back on.         |
|                                      |
+--------------------------------------+
| Home | Apps & keys | Library | More   | 64 + safe area
+--------------------------------------+
```

The image restriction is the only place that fact appears; there is no contradictory “Images available” chip. No endpoint or key is exposed until the member selects connection setup. A successful setup ends with the actual result and a receipt link, not just a green connection badge.

**Status: 1440 × 900 px; 208 px navigation, 24 px content gutters.**

```text
+------------------+-----------------------------------------------------------------------+
| Azizam           | [Search]                                                   [Account] | 56
| Home             +-----------------------------------------------------------------------+
| Apps & keys      | Status                                                                |
| Try              | Images unavailable                                                    |
| Library          | Engine: Studio stopped.  [Check engine]              Checked 1m ago   |
|                  |                                                                       |
| [Admin v]        | Capability history, last 7 days                 [Accessible table]    |
| > Status         |        Wed   Thu   Fri   Sat   Sun   Mon   Tue                         |
| Engines          | Chat   ====  ====  ====  ====  ====  ====  ====                        |
| Providers        | Images ====  ====  ====  ====  ====  ====  ==xx                        |
| People           | Voice  ====  ====  ====  ====  ====  ====  ====                        |
| Usage            | Video  ....  ....  ....  ====  ====  ====  ====                        |
| Activity         | = Successful check   x Failed check   . No check saved                |
| Settings         |                                                                       |
|                  | Other paths                       | Resolved incidents                |
|                  | Name          Layer     State     | Monday                            |
|                  | Requests      Gateway   Working   | Provider timeout resolved         |
|                  | Key check     Gateway   Working   | [Open receipt]                    |
|                  | Chat          Engine    Working   |                                   |
|                  | Voice         Engine    Working   | Sunday                            |
|                  | Transcription Engine    Working   | Voice engine restarted            |
|                  | Embeddings    Engine    Working   | [Open receipt]                    |
|                  | Remote calls  Provider  Unchecked |                                   |
|                  | Image edit    Workflow  Paused    |                                   |
|                  | [Inspect selected path]           |                                   |
+------------------+-----------------------------------------------------------------------+
```

The affected engine belongs to the opening incident, not a duplicate row below. Each other path has its own evidence time in the implemented table; the one global timestamp above describes the incident check only. History cells represent distinct dated checks, not an uptime promise; the final image cell is the current sample, whose explanation lives above. The matrix itself is not interactive; Accessible table exposes the records with 44 px actions. A check-failure colour does not identify its cause until the receipt does. On a phone, history becomes per-capability summaries with a details action; the verdict, layer and action remain first.

**Library: 1440 × 900 px; 208 px navigation, 24 px gutters.**

```text
+------------------+-----------------------------------------------------------------------+
| Azizam           | [Search]                                                   [Account] | 56
| Home             +-----------------------------------------------------------------------+
| Apps & keys      | Library                        [Search creations] [Type: All v]       |
| Try              |                                                                       |
| > Library        | Browse, 224 px | Selected creation, 632 px       | Details, 264 px     |
|                  |                |                                 |                    |
| [Admin v]        | > [thumbnail]  | +-----------------------------+ | Image              |
|                  |   Today 14:12  | |                             | | PNG file           |
|                  |   [thumbnail]  | |       actual creation       | | 1536 x 1024        |
|                  |   Today 13:40  | |                             | |                    |
|                  |   [thumbnail]  | |                             | | [Download]         |
|                  |   Monday       | |                             | | [Open receipt]     |
|                  |   [audio icon] | |                             | |                    |
|                  |   Monday       | +-----------------------------+ | Cost not recorded  |
|                  |                | Garden study                    |                    |
|                  |                | [Previous] [Next]               |                    |
|                  |                |                                 |                    |
|                  |                | Video/audio: transport replaces |                    |
|                  |                | this row; never autoplay.       |                    |
+------------------+-----------------------------------------------------------------------+
```

Content width is 1184 px: 224 + 24 gap + 632 + 24 gap + 264. Titles are in the selected viewer only; other list rows use their own titles when available, while the selected row uses its timestamp to avoid repeating “Garden study.” Exact request/model strings live in the receipt, not permanent decorative labels. Fewer items leave a useful selected viewer, not a stretched empty table.

### Self-check and revision

**Would I produce this for any similar product?** The compact shell is transferable; pretending otherwise would be dishonest. I would not default a generic proxy to this particular combination of household action Home, fin-art crop, capability/check history, evidenced engine/provider/workflow layers and media/receipt viewer. Those decisions come from Azizam's split audience, multi-capability output and explicit handoff authority, not from a stock observability dashboard.

**Which ban-list items is it closest to, and how does it avoid them?** A5 is the main risk because the handoff has dark panels and telemetry; A2 is a second risk, and R14's repeated metric boxes approach A4. I revised away navy-black and all blue-violet washes, chose a visibly green-blue field with a muted blue accent and several semantic colours, removed the KPI-tile band, and limited history to one shared matrix supported by records. Borders define unequal working regions rather than identical rounded cards; the light version uses no serif or earth tones, and the table is not an A3 newspaper grid. B6/B10/B14/B15 are explicitly removed rather than excused by the handoff. Remaining risk: the art crop and dense matrix must be judged together in a render; tokens alone cannot establish that the page has escaped A5.

## Direction 2: Workspace

**Default:** light. **Background:** `#FFFFFF`. **Accent:** `#2855B6`. **Display:** Archivo. **UI:** Source Sans 3. **Numerals:** Source Sans 3 with tabular figures. **Copied identifiers/code:** Source Code Pro. **Rationale:** a bright working surface makes family tasks immediately legible, while one bounded viewing area carries the handoff's contrast and depth.

**Real-world referent:** a photography light table beside a dark viewing monitor: the white surface holds named items and controls; the bounded monitor holds the thing being examined. This is a layout relationship, not a texture treatment or a camera-themed product. It translates the approved high contrast, brand art, dense information and right-context grammar into daylight use.

### References and the traits taken

| Board ID | Specific trait taken from the opened image | Application |
| --- | --- | --- |
| R01 | Labels, actions and present state stay close; mobile preserves grouping | Home is a compact list of things a person can do, with restrictions beside the relevant action |
| R08 | The question/answer precedes architectural detail | Connection begins with app choice and usable setup, not an explanation of protocols |
| R03 | Small verdict region, grouped rows and disclosure | Status makes the current consequence readable before the capability inventory |
| R18 | Incident explanation has useful width before evidence tables | One expanded incident region owns cause and next action; no second incident card underneath |
| R10 | One shared chart under related readings, with keyed units and periods | An incident's actual request outcomes occupy one bounded evidence area, not a chart per card |
| R17 | Strong focal artwork within a bounded mobile opening composition | Home uses one small deep-water art inset |
| R19 | The opening status region groups the verdict and subordinate evidence | Status keeps one bounded region for the consequence and request evidence, with no second fish artwork |
| R11 | Artwork earns more visual weight than surrounding browsing controls | Library shows actual creations in a contact sheet with one selected preview, without recommendations or genre chips |
| R12 | Image/video viewer has edge controls and browsing context | Selected media opens prominently with visible transport; hover is never required |
| R20 | Selection and object detail are adjacent | Library's preview rail belongs to the selected creation and becomes a dedicated phone view |

### Palette and measured contrast

Exactly six UI colours per variant. Unknown uses the ordinary text colour plus a question mark and written explanation, not a washed-out disabled style. Accent is reserved for actions/selection. The white default is deliberately cool and high contrast; there is no warm cream, serif face or earthy brand accent.

| Role | Light hex | Contrast on `#FFFFFF` | Dark hex | Contrast on `#182130` |
| --- | --- | ---: | --- | ---: |
| Background | `#FFFFFF` | — | `#182130` | — |
| Text / unknown | `#172536` | 15.51:1 | `#EDF2FA` | 14.38:1 |
| Accent | `#2855B6` | 6.85:1 | `#A7BBF5` | 8.52:1 |
| OK | `#206246` | 7.25:1 | `#8ECEB1` | 8.94:1 |
| Attention | `#895316` | 6.34:1 | `#E9BD78` | 9.25:1 |
| Failed | `#AD3545` | 6.23:1 | `#F19AA8` | 7.64:1 |

Computed using the same WCAG luminance equation above; all four status colours pass normal-text AA in both variants. Buttons use accent fill with the variant background for text: 6.85:1 light, 8.52:1 dark. The light variant's dark Home art inset uses `#172536` with `#FFFFFF` for any edge label, 15.51:1; it contains no status text. Status colours stay on the page background, including chart labels and legends. Dark mode preserves topology, swaps the whole UI palette and retains actual creation colours; it does not invert images. No secondary text opacity reduces these ratios.

### Type and construction

Sizes are CSS pixels, shown as size/line-height. Archivo is used for short page titles and the active consequence; its broad forms contrast with Console's condensed verdict.

| Role | Face | Desktop | Phone | Weight | Tracking |
| --- | --- | --- | --- | ---: | --- |
| Display / active consequence | Archivo | 36/42 | 30/36 | 650 | -0.02em |
| h1 / page title | Archivo | 30/36 | 28/34 | 600 | -0.015em |
| h2 / region title | Source Sans 3 | 22/28 | 20/26 | 600 | 0 |
| Body / controls | Source Sans 3 | 16/24 | 16/24 | 400; controls 600 | 0 |
| Small / metadata | Source Sans 3 | 14/20 | 14/20 | 400; labels 600 | 0 |
| Numeral / readings | Source Sans 3, tabular | 22/28; tables 16/24 | 20/26; rows 16/24 | 600 | 0 |
| Exact model name, URL, request ID, code | Source Code Pro | 14/20 | 14/20 | 400 | 0 |

No uppercase navigation or tracked-caps table headers. Unknown is readable text, not low-contrast grey. Font loading and language coverage remain implementation checks, not established by these diagrams.

- **Spacing:** 4, 8, 12, 16, 24, 32, 48, 64 px. Phone gutters 16; desktop gutters 32. Desktop sidebar 184, top rail 56. Action rows 56 px, operational rows 52 px. Library thumbnails use 12 px gaps rather than roomy card padding. Home has slightly more separation between tasks; Status remains dense where an owner needs evidence.
- **Radius:** controls 6 px; the single task/preview enclosure 12 px; creation thumbnails 2 px; ordinary inventory/list rows have no enclosing card. A 12 px art enclosure is not repeated around every item.
- **Elevation:** border only; no shadows. Controls have full text-colour outlines; the selected item has a 2 px accent outline around its whole perimeter. One 1 px border at 28% text opacity separates major regions; rows use spacing and alignment rather than hairlines everywhere. Focus adds a full accent 2 px outline with a 2 px gap. No coloured left-edge incident stripe.
- **Icons:** one 20 px outline family, 2 px stroke, gently rounded joins, filled selection indicator only. Phone navigation is 24 px with labels. Distinct destination silhouettes follow the shared shell rule; no icons attached to every heading. State icons remain shape-specific and labelled.
- **Theme behaviour:** white dominates the default shell, lists, forms and evidence region. The Home art inset is bounded to 96 × 96 px on a phone; actual media supplies Library colour. A dark user preference produces a slate workspace with the same broad type and contact-sheet layout, not Console's green-blue board.

### First viewport and the one bold element

| Surface | What the reader learns in two seconds | Single memorable element | What stays quiet |
| --- | --- | --- | --- |
| Home, 390 px | The available task can be opened, or a familiar app can be connected, with no model-name decision yet | One square deep-water brand-art inset beside the Home title, within the otherwise white task region | A vertical action list; each restriction sits with its capability; no metric cards or large greeting |
| Status, 1440 px | Images are affected; the current incident explains the layer and action, while other capabilities are separately readable | One wide evidence plot integrated into the expanded incident, showing actual request outcomes over time | Capability disclosure at left, resolved incidents below, numerical context as one inline row rather than tiles |
| Library, 1440 px | Their work is browsable by date, and one selected item can be played/opened without losing the collection | One contact sheet of real creations; the sheet, not a branded hero, supplies the visual pattern | A narrower selected-preview rail with file controls; titles stay ordinary size and filters occupy one toolbar |

The Status chart is the sole bold evidence element; its verdict uses clear 36 px type but no second colour block or illustration. With insufficient records, the chart disappears and the consequence sentence becomes the only emphasis. An empty Library uses a narrow useful explanation rather than six placeholder boxes. No creation is enlarged just because it is bright: selection is explicit, keyboard accessible and preserved across filtering.

### Wireframes

**Home: 390 × 844 px; 16 px gutters, 358 px content.**

```text
+--------------------------------------+
| Azizam                       [Account]| 56
+--------------------------------------+
| +----------------------------------+ |
| | Home              +------------+ | |
| |                   | deep-water | | |
| |                   | brand art  | | |
| |                   |   96 x 96  | | |
| |                   +------------+ | |
| | Chat is ready.                   | |
| | [Open chat]                      | | 48 high
| | [Connect an app]                 | | 48 high
| +----------------------------------+ |
|                                      |
| Other things you can do              |
| Voice                     [Open]     | 56 high
| Images are paused.                   |
| Ask Sam to turn them back on.         |
|                                      |
| Your apps                            |
| Daisy                     [Open]     | 56 high
| LibreChat                 [Open]     | 56 high
|                                      |
| Recent work                   [View] |
+--------------------------------------+
| Home | Apps & keys | Library | More   | 64 + safe area
+--------------------------------------+
```

The art region occupies about 3% of a 390 × 844 screen; it is deliberately smaller than the actionable region. “Other things you can do” contains actual available actions plus adjacent restrictions, not decorative capability chips. App Open controls appear only when an app has a real launch target; otherwise the row offers setup. Missing recent work shortens this view without inventing a chart to fill it.

**Status: 1440 × 900 px; 184 px navigation, 32 px gutters.**

```text
+----------------+-------------------------------------------------------------------------+
| Azizam         | [Search]                                                     [Account] | 56
| Home           +-------------------------------------------------------------------------+
| Apps & keys    | Status                                                                  |
| Try            |                                                                         |
| Library        | Capabilities, 280 px | Current incident, 884 px                         |
|                |                      |                                                  |
| [Admin v]      | > Images             | Images unavailable                               |
| > Status       |   Selected           | Engine: Studio stopped.  [Check engine]          |
| Engines        | Chat       Working   | Checked 1m ago                                   |
| Providers      | Voice      Working   | Requests per 5-minute interval, past hour        |
| People         | Video      Unchecked | Count  10 |     __ __                            |
| Usage          | Music      Working   |         5 | ___       ____                       |
| Activity       | Sound effects Working|         0 |___________________ ___               |
| Settings       | Embeddings Working   |            13:00              14:00              |
|                | Transcription Working| Successful / Failed; missing samples are gaps    |
|                |                      | [Open matching receipts]                         |
|                | [Inspect layers]     |                                                  |
|                |                      | Spend: Cost not recorded                         |
|                |                      |                                                  |
|                |                      | Resolved incidents                               |
|                |                      | Monday  Provider timeout        [Open receipt]   |
|                |                      | Sunday  Voice engine restart    [Open receipt]   |
+----------------+-------------------------------------------------------------------------+
```

Content width is 1192 px: 280 + 28 gap + 884. The selected Images row names the object; only its expanded region states the failure, layer and next action. The implemented plot uses two aligned lanes for successful and failed requests, a shared time axis and count scale, distinct solid/dashed traces and a table alternative; the ASCII compresses those lanes into one schematic plot. Its keys are a passive legend in ordinary text, not toggle-shaped buttons. Each capability has its own evidence age on disclosure. Unchecked Video opens its own check action, not this unrelated image incident. On a phone, selected consequence/action comes before the capability list and the plot becomes a readable summary plus details.

**Library: 1440 × 900 px; 184 px navigation, 32 px gutters.**

```text
+----------------+-------------------------------------------------------------------------+
| Azizam         | [Search]                                                     [Account] | 56
| Home           +-------------------------------------------------------------------------+
| Apps & keys    | Library                       [Search creations] [Type: All v]          |
| Try            |                                                                         |
| > Library      | Collection, 768 px                           | Preview, 400 px           |
|                | Today                                        |                          |
| [Admin v]      | +----------------+ +----------------+ +-----+ | +----------------------+ |
|                | | selected image | | actual image   | |audio| | | actual selected      | |
|                | |                | |                | |file | | | creation             | |
|                | +----------------+ +----------------+ +-----+ | |                      | |
|                | 14:12               Lake sketch       Voice  | +----------------------+ |
|                |                                       0:34  | Garden study             |
|                |                                            | [Open full view]         |
|                | Monday                                     | [Download] [Receipt]     |
|                | +----------------+ +----------------+ +-----+ |                          |
|                | | actual video   | | actual image   | |audio| | Image                    |
|                | |                | |                | |file | | 1536 x 1024              |
|                | +----------------+ +----------------+ +-----+ | Cost not recorded        |
|                | Lanterns            Desk study       Intro  |                          |
|                | 0:12                                  0:20  | Timed media:             |
|                |                                            | [Play] [Seek] [Sound]    |
+----------------+-------------------------------------------------------------------------+
```

Content width is 1192 px: 768 + 24 gap + 400. The collection uses three 248 px cells with 12 px gaps, consistent 160 px preview heights and uncropped aspect-fit images; ASCII cell widths are schematic. Audio cells use the file's actual title and a play control, with a waveform only when extracted from the file. One selection outline around the full thumbnail identifies the detail target without implying state. The selected item's title lives only in the preview rail; every other title stays with its thumbnail. The full viewer gives media at least 65% of the content area and makes play/seek/sound controls visible. On a phone, the sheet is two columns and selection opens a dedicated view; there is no squeezed right rail.

### Self-check and revision

**Would I produce this for any similar product?** A light table/contact sheet would suit many creative tools; that referent alone is insufficient. I revised Home away from a generic workspace greeting to app connection plus evidenced capability actions, and Status away from a gallery of metrics to the selected incident's layer, request outcomes and receipts. The bounded deep-water art, provider-charge language, ordinary-member shell and creation-to-receipt relationship make this a proposal for Azizam's particular product, although the typography and controls remain intentionally familiar.

**Which ban-list items is it closest to, and how does it avoid them?** A4 is the closest light-mode risk; A3 is the next because white backgrounds and aligned rows can slide into ruled editorial styling. I removed individual card wrappers from action rows and collection cells, kept borders at major boundaries rather than every line, used variable radii by role and broad sans type without newspaper hairlines or serif headings. In dark mode the bounded slate workspace could approach A5; it has no gradient glow, KPI tiles or decorative charts, and media rather than blue lighting supplies richness. B6/B7/B10/B13/B15 and C18/C20/C21 remain banned regardless of the reference. Remaining risk: the owner's strongest approval is dark; this daylight translation meets the requested non-dark alternative but has not earned owner approval, and must be judged alongside its dark variant.

## Difference and choice

Console uses a persistent three-region operating frame, condensed verdict type and aligned check history; Workspace uses a selected-incident reading area and a contact sheet with broader type. Console's colour strategy is a continuous dark green-blue field with muted blue actions, while Workspace defaults to white with cobalt actions and confines deep-water atmosphere to the Home artwork. Console packs evidence into 48 px rows and centres Library on one creation, whereas Workspace uses 52–56 px task rows and gives the collection more area than the preview.

I would choose Console because its default atmosphere follows the owner's strongest visual approval and its shared check history gives Status a distinctive, useful focal element. I would retain its light variant for daylight use and require phone task testing plus four-theme rendered review before claiming that either direction is ready to ship.

Confidence is high that both specifications satisfy the stated palette, voice and structural constraints; confidence in visual quality is provisional until rendered, and a non-technical household member's unaided success has not yet been tested.
