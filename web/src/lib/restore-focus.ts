/** Restore an opener if it can still receive focus, then try surviving controls. */
export function restoreFocus(
  invoker: HTMLElement | null,
  fallbackFocus?: () => readonly (HTMLElement | null)[],
): void {
  function focus(element: HTMLElement | null): boolean {
    if (
      !element?.isConnected ||
      element.matches(":disabled") ||
      element.closest("[hidden], [inert]")
    ) {
      return false;
    }
    element.focus();
    // A connected element can still be hidden or otherwise unfocusable.
    return element.ownerDocument.activeElement === element;
  }

  if (focus(invoker)) return;
  for (const candidate of fallbackFocus?.() ?? []) {
    if (focus(candidate)) return;
  }
}
