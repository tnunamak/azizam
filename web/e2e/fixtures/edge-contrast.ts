import type { Locator, Page } from "@playwright/test";

/**
 * The contrast, as WCAG 2 measures it, of an element's edge against the page: the border colour composited over the
 * element's own fill, against the page's background. The browser's computed colours are painted on a canvas, so a
 * colour the page writes in another space (oklab, color-mix) is resolved the way the screen shows it.
 */
export async function edgeContrast(page: Page, locator: Locator): Promise<number> {
  const colours = await locator.evaluate((element) => ({
    edge: getComputedStyle(element).borderTopColor,
    fill: getComputedStyle(element).backgroundColor,
    page: getComputedStyle(document.body).backgroundColor,
  }));
  return page.evaluate((input) => {
    const context = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
    const pixel = (...paints: string[]) => {
      context.clearRect(0, 0, 1, 1);
      for (const paint of paints) {
        context.fillStyle = paint;
        context.fillRect(0, 0, 1, 1);
      }
      return Array.from(context.getImageData(0, 0, 1, 1).data.slice(0, 3));
    };
    const luminance = ([red, green, blue]: number[]) => {
      const linear = (channel: number) => {
        const value = channel / 255;
        return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue);
    };
    const edgeOnFill = luminance(pixel(input.fill, input.edge));
    const page = luminance(pixel(input.page));
    return (Math.max(edgeOnFill, page) + 0.05) / (Math.min(edgeOnFill, page) + 0.05);
  }, colours);
}
