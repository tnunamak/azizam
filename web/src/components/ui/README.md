# Phone target sizes

Buttons (including copy buttons and icon buttons),
switches and tab triggers reserve a minimum 44 × 44 CSS pixel hit region below
640px. The minimum applies to every size, including `sm`, `xs` and icon sizes,
and to buttons rendered as links with `asChild`. Size props select the compact
desktop appearance, not a smaller phone target.

Switches keep their compact visual track inside the clickable, focusable Radix
root. Both the thumb and the transparent padding activate that same root; there
is no second tab stop or overlapping pseudo-element target. Disabled switches
remain disabled across the whole region. Focus rings surround the hit region.

Tab lists and triggers reserve at least 44px below 640px.
Arrow-key navigation and disabled-tab semantics stay with Radix.

`CompactTouchTargets` applies the same 44 × 44 CSS pixel minimum below 640px
to controls in routes that opt into it, including portaled dialog controls.
Desktop size semantics stay unchanged.

Consumers must allow controls to wrap with space between them. Do not override
these minimum dimensions to fit a crowded row; adjust the page layout instead.
These contracts cover the named primitives, not arbitrary native buttons,
links, inputs or controls in the shell.
