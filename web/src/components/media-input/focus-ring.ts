/**
 * The keyboard focus ring of every control in the media input and its Library dialog: 2 px in `--focus-ring` (the dark
 * teal of Run and Make) with a 2 px gap, so it reads against the dialog, the page and a hover fill alike. The gap is
 * why a control that is not the page's own surface (a tile in the scrolling list) keeps 4 px of room around it.
 */
export const FOCUS_RING =
  "focus-visible:ring-focus-ring focus-visible:ring-offset-background focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none";
