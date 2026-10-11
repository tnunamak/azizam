import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import {
  VoicePicker,
  selectedVoice,
  type SpeechVoices,
} from "@/app/try/voice-picker";

const choices: SpeechVoices = {
  voices: [
    { id: "F1", name: "F1" },
    { id: "M1", name: "M1" },
  ],
  default_voice: "F1",
  allow_other: false,
};
afterEach(cleanup);

test("selects the model default and offers only its voices", () => {
  const change = vi.fn();
  render(
    <VoicePicker
      choices={choices}
      value={selectedVoice(choices)}
      onChange={change}
      loading={false}
    />,
  );
  expect(screen.getByRole("combobox", { name: "Voice" })).toHaveValue("F1");
  expect(
    screen.getAllByRole("option").map((option) => option.textContent),
  ).toEqual(["F1", "M1"]);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "M1" } });
  expect(change).toHaveBeenCalledWith("M1");
  expect(selectedVoice(choices, "another-model-voice")).toBe("F1");
});

test("loading and empty lists are disabled and do not invent voices", () => {
  const { rerender } = render(
    <VoicePicker value="" onChange={() => {}} loading />,
  );
  expect(screen.getByRole("combobox")).toBeDisabled();
  expect(screen.getByRole("combobox")).toHaveAttribute("aria-busy", "true");
  expect(screen.getByText("Loading voices…")).toBeInTheDocument();
  rerender(<VoicePicker value="" onChange={() => {}} loading={false} />);
  expect(screen.getByRole("combobox")).toBeDisabled();
  expect(
    screen.getByText(/Your request will use its default voice/),
  ).toBeVisible();
});

test("an explicit pass-through contract enables Other voice", () => {
  render(
    <VoicePicker
      choices={{ ...choices, allow_other: true }}
      value="custom"
      onChange={() => {}}
      loading={false}
    />,
  );
  expect(
    screen.getByRole("option", { name: "Other voice" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "Voice ID" })).toHaveValue(
    "custom",
  );
});
