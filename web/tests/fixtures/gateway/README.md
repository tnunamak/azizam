# Future gateway fixture seam

Browser tests in this foundation use only `tests/playwright/static-smoke.html`.
They do not start or contact the gateway, use ports, or read operator state.

An authenticated gateway bootstrap, when it is needed, belongs in this
directory as a dedicated fixture. It must provide its own isolated lifecycle
and credentials; it must not be added to the default Playwright configuration.
