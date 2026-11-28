import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ConnectionFacts } from "./contract-sections";
import { definitions, engineView } from "../__fixtures__/test-utils";
afterEach(cleanup);
it("does not claim an unread stored key will be sent", () => {
  const view = engineView({
    key: {
      state: "configured",
      source: "stored",
      origin: "http://127.0.0.1:8080",
      availability: "unknown",
    },
    request_path: {
      mode: "direct",
      source: "service record",
      url: "http://127.0.0.1:8080/v1/chat/completions",
    },
  });
  render(
    <ConnectionFacts view={view} definition={definitions.definitions[0]} />,
  );
  expect(screen.getByTestId("connection-facts")).toHaveTextContent(
    "Availability is checked when the key is used",
  );
  expect(screen.getByTestId("connection-facts")).not.toHaveTextContent(
    "Azizam never sends it",
  );
});
it("reports unclassified settings without showing their values", async () => {
  const { StoredRecordSection } = await import("./record-sections");
  render(
    <StoredRecordSection
      view={engineView({ hidden_settings: 2 } as never)}
    />,
  );
  expect(
    screen.getByText("2 settings hidden: not classified"),
  ).toBeInTheDocument();
});
it("describes configured command arguments without object coercion", async () => {
 const { argvText } = await import("./contract-sections");
 expect(argvText(["runner", { id: "ent_arg", configured: true }] as never)).toBe("runner (configured secret)");
});
