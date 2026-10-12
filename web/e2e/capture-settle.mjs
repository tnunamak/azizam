// What the manual capture scripts (d1-*, household-capture, prompt-memory-capture)
// wait on instead of timers or network quiet: loading placeholders gone, fonts
// ready, every finite animation finished, two frames painted.
const shown = el => {
  const style = getComputedStyle(el);
  return style.display !== "none" && style.visibility !== "hidden" && el.getBoundingClientRect().width > 0;
};
export const skeletonsGone = () =>
  ![...document.querySelectorAll('[aria-busy="true"], [data-slot="skeleton"]')].some(shown);

export async function quiet(page, { timeout = 20_000 } = {}) {
  await page.waitForFunction(skeletonsGone, null, { timeout });
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(document.getAnimations()
      .filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity)
      .map(animation => animation.finished.catch(() => {})));
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
}

/** rm -f without the force flag: a missing file is fine, anything else is not. */
export async function removeIfPresent(remove, path, options = {}) {
  await remove(path, options).catch(error => { if (error.code !== "ENOENT") throw error; });
}
