/** Full-page navigation to another origin, kept in one place so tests can
 * watch it instead of leaving the test page. */
export const navigate = {
  to(url: string) {
    window.location.assign(url);
  },
};
