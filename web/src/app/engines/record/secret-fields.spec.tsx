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
