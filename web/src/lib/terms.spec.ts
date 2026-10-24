import { expect, it } from "vitest";
import { capabilityLabels } from "./terms";

it("names the speech capability Voice, as the VOICE glossary does", () => {
  expect(capabilityLabels.speech).toBe("Voice");
});
