import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { SecretFields } from "./secret-fields";
import { secretDrafts } from "./secret-edits";
afterEach(cleanup);
function Editor() {
  const [drafts, setDrafts] = useState(() =>
    secretDrafts({
      config: {
        headers: [{ id: "ent_h", name: "X-Private", configured: true }],
        env: { TOKEN: { id: "ent_e", configured: true } },
      },
      process: { start: ["runner", { id: "ent_a", configured: true }] },
    }),
  );
  return (
    <SecretFields
      drafts={drafts}
      onChange={setDrafts}
      part="all"
      disabled={false}
    />
  );
}
it("keeps configured values empty and makes replace and clear explicit", async () => {
  render(<Editor />);
  const user = userEvent.setup();
  expect(screen.getByLabelText("Value for X-Private")).toHaveValue("");
  await user.type(screen.getByLabelText("Value for X-Private"), "replacement");
  expect(screen.getByText("Replacement pending")).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "Remove header X-Private" }),
  );
  expect(screen.getByText(/Removal pending/)).toBeVisible();
});
it("shows a program and one literal field plus secret toggle per argument", async () => {
  render(<Editor />);
  const user = userEvent.setup();
  expect(screen.getByLabelText("process start command Program")).toHaveValue(
    "runner",
  );
  expect(screen.getByLabelText("process start command Argument 1")).toHaveValue(
    "",
  );
  expect(
    screen.getByText(/Shell quoting and expansion do not run/),
  ).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "Add process start command argument" }),
  );
  expect(screen.getByLabelText("process start command Argument 2")).toHaveValue(
    "",
  );
  expect(
    screen.getByRole("checkbox", {
      name: "Secret process start command Argument 2",
    }),
  ).not.toBeChecked();
});
it("env is a write-only input with configured metadata", () => {
  render(<Editor />);
  expect(screen.getByLabelText("Value for TOKEN")).toHaveValue("");
  expect(screen.getByLabelText("Value for TOKEN")).toHaveAttribute(
    "type",
    "password",
  );
});
it("can hide legacy header carriers when the saved dispatch does not use them", () => {
  const drafts = secretDrafts({ config: { headers: [{ id: "ent_h", name: "X-Legacy", configured: true }] } });
  render(<SecretFields drafts={drafts} onChange={() => {}} part="connection" disabled={false} excludeKinds={["headers"]} />);
  expect(screen.queryByRole("group", { name: "Request headers" })).toBeNull();
});
it("cancels a secret-toggle edit by restoring the original argument", async () => {
  render(<Editor />);
  const user = userEvent.setup();
  const toggle = screen.getByRole("checkbox", {
    name: "Secret process start command Argument 1",
  });
  await user.click(toggle);
  await user.type(
    screen.getByLabelText("process start command Argument 1"),
    "public-draft",
  );
  await user.click(screen.getByRole("button", { name: "Cancel replacement" }));
  expect(toggle).toBeChecked();
  expect(screen.getByLabelText("process start command Argument 1")).toHaveValue(
    "",
  );
});
function UrlEditor() {
  const [drafts, setDrafts] = useState(() =>
    secretDrafts({ config: { base_url: "http://localhost:8080/v1" } }),
  );
  return (
    <SecretFields
      drafts={drafts}
      onChange={setDrafts}
      part="connection"
      disabled={false}
    />
  );
}
it("can cancel URL input without discarding other drafts", async () => {
  render(<UrlEditor />);
  const user = userEvent.setup();
  const input = screen.getByLabelText("config.base_url replacement URL");
  await user.type(input, "http://user:pass@localhost:8080/v1");
  await user.click(screen.getByRole("button", { name: "Cancel replacement" }));
  expect(input).toHaveValue("");
});
