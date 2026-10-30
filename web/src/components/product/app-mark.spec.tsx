import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AppMark, appMonogram } from "./app-mark";

afterEach(cleanup);

describe("an app's mark", () => {
  it("draws the published brand mark when Azizam has one", () => {
    const { container } = render(<AppMark app="Codex" />);
    expect(container.querySelector("svg path")).not.toBeNull();
    expect(container.textContent).toBe("");
  });

  it("gives an app with no mark its own monogram, so two such apps differ", () => {
    expect(appMonogram("Open WebUI")).toBe("OW");
    expect(appMonogram("LibreChat")).toBe("LC");
    expect(appMonogram("Daisy")).toBe("D");
    expect(appMonogram("codex")).toBe("C");
    const { container } = render(<AppMark app="Daisy" />);
    expect(container.querySelector("svg")).toBeNull();
    expect(container.textContent).toBe("D");
  });
});
