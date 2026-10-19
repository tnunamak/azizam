import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";
import {
  AVATAR_PICTURE_PATH,
  AvatarPicker,
  PersonAvatar,
  choiceFromSelection,
  generatedAvatarPalettes,
  getAvatarInitials,
  getAvatarPictureUrl,
  getGeneratedAvatar,
  selectionFromChoice,
} from "./person-avatar";
import { PersonMark } from "./person-mark";

afterEach(cleanup);

const PICTURE = `${AVATAR_PICTURE_PATH}?v=1760000000`;
function discOf(container: HTMLElement) {
  const root = container.firstElementChild as HTMLElement;
  return { root, background: (root.firstElementChild as HTMLElement).style.backgroundImage };
}

function luminance(hex: string) {
  const channel = (offset: number) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

describe("generated avatars", () => {
  it("draws the same disc for the same person id, whatever the name says", () => {
    const before = render(<PersonAvatar personId="usr_a1b2" name="Mira" />);
    const first = discOf(before.container).background;
    before.unmount();
    const after = render(<PersonAvatar personId="usr_a1b2" name="Mira Chen-Okafor" />);
    expect(discOf(after.container).background).toBe(first);
    expect(first).not.toBe("");
  });

  it("seeds from the id and not the name: two ids with one name differ somewhere", () => {
    const fills = new Set(
      Array.from({ length: 40 }, (_, index) => {
        const { container, unmount } = render(
          <PersonAvatar personId={`usr_${index}`} name="Sam" />,
        );
        const result = discOf(container).background;
        unmount();
        return result;
      }),
    );
    expect(fills.size).toBeGreaterThan(8);
  });

  it("is a pure function of the seed, and the name never reaches it", () => {
    expect(getGeneratedAvatar("usr_1")).toEqual(getGeneratedAvatar("usr_1"));
    expect(getGeneratedAvatar("usr_1").id).toBe(getGeneratedAvatar("usr_1").id);
    // Every palette is reachable from some id.
    const reached = new Set(
      Array.from({ length: 400 }, (_, index) => getGeneratedAvatar(`usr_${index}`).id),
    );
    expect(reached.size).toBe(generatedAvatarPalettes.length);
  });

  it("uses the name only when a caller has no person id", () => {
    expect(
      discOf(render(<PersonAvatar name="Mira" />).container).background,
    ).toBe(discOf(render(<PersonAvatar name="  MIRA " />).container).background);
  });

  it("honours a chosen variant over the seed", () => {
    const { container } = render(
      <PersonAvatar personId="usr_a1b2" name="Mira" selection={{ kind: "generated", variant: "v4" }} />,
    );
    const rose = generatedAvatarPalettes[3];
    expect(rose.id).toBe("v4");
    expect(discOf(container).background).toContain("#673D52");
    expect(getGeneratedAvatar("usr_a1b2", "v4").from).toBe(rose.from);
  });

  it("keeps white initials readable on every gradient stop (4.5:1)", () => {
    for (const palette of generatedAvatarPalettes) {
      for (const stop of [palette.from, palette.to]) {
        expect(1.05 / (luminance(stop) + 0.05), `${palette.label} ${stop}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("shows one initial at every size, the preview included", () => {
    for (const size of ["2xs", "xs", "sm", "md", "lg", "xl"] as const) {
      const { container, unmount } = render(
        <PersonAvatar personId="u" name="Yara Adeyemi" size={size} decorative />,
      );
      expect(container).toHaveTextContent(/^Y$/);
      unmount();
    }
  });

  it("names itself for a screen reader unless an adjacent label does", () => {
    render(<PersonAvatar personId="u" name="Mira Chen" />);
    expect(screen.getByRole("img", { name: "Mira Chen" })).toBeVisible();
    cleanup();
    const { container } = render(<PersonAvatar personId="u" name="Mira Chen" decorative />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <PersonAvatar personId="u" name="Mira Chen" />
        <AvatarPicker personId="u" name="Mira Chen" pictureUrl={PICTURE} onChange={() => {}} />
      </div>,
    );
    expect((await axe(container)).violations).toEqual([]);
  });
});

describe("initials", () => {
  it.each([
    ["Mira Chen", "M"],
    ["mira", "M"],
    ["  ", "?"],
    ["", "?"],
    ["Émile Zola", "É"],
    ["éclair", "É"],
    ["李小龍", "李"],
    ["  ada lovelace", "A"],
    ["123 Main", "1"],
    ["@@@", "?"],
    ["@sam", "S"],
  ] as const)("%j is %j", (name, expected) => {
    expect(getAvatarInitials(name)).toBe(expected);
  });
});

describe("the sign-in picture", () => {
  it.each([
    [PICTURE, PICTURE],
    [AVATAR_PICTURE_PATH, AVATAR_PICTURE_PATH],
    [`${AVATAR_PICTURE_PATH}?person=usr_1&v=2`, `${AVATAR_PICTURE_PATH}?person=usr_1&v=2`],
  ])("accepts the gateway path %s", (value, expected) => {
    expect(getAvatarPictureUrl(value)).toBe(expected);
  });

  it.each([
    "https://lh3.example.test/a/photo.png",
    "http://lh3.example.test/a/photo.png",
    "//lh3.example.test/a/photo.png",
    `https://evil.example${AVATAR_PICTURE_PATH}`,
    "/me/api/profile/avatar",
    "/me/api/profile/avatar/picture/../../session",
    "/other.png",
    "data:image/png;base64,AAAA",
    "javascript:alert(1)",
    `${AVATAR_PICTURE_PATH}\\evil`,
    `${AVATAR_PICTURE_PATH} x`,
    `${AVATAR_PICTURE_PATH}\n`,
    "",
    null,
    undefined,
  ])("refuses %j: the browser never loads another address", (value) => {
    expect(getAvatarPictureUrl(value as string | null | undefined)).toBeUndefined();
  });

  it("loads the picture only when the person chose it", () => {
    const chosen = render(
      <PersonAvatar personId="u" name="Mira" selection={{ kind: "sign-in" }} pictureUrl={PICTURE} />,
    );
    const root = chosen.container.firstElementChild as HTMLElement;
    expect(root).toHaveAttribute("data-avatar-selection", "sign-in");
    chosen.unmount();
    for (const selection of [null, { kind: "auto" }, { kind: "generated", variant: "v2" }] as const) {
      const other = render(<PersonAvatar personId="u" name="Mira" selection={selection} pictureUrl={PICTURE} />);
      expect(other.container.querySelector("img")).toBeNull();
      expect(other.container.firstElementChild).toHaveAttribute("data-avatar-selection", "generated");
      other.unmount();
    }
  });

  it("never loads a provider address, even when asked to", () => {
    const { container } = render(
      <PersonAvatar personId="u" name="Mira" selection={{ kind: "sign-in" }} pictureUrl="https://lh3.example.test/p.png" />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.firstElementChild).toHaveAttribute("data-avatar-selection", "generated");
    expect(container.innerHTML).not.toContain("lh3.example.test");
  });

  it("asks the gateway for the picture with no referrer, and swaps it in once it has loaded", async () => {
    const requests: { src: string; referrerPolicy: string }[] = [];
    class FakeImage extends EventTarget {
      referrerPolicy = "";
      crossOrigin: string | null = null;
      complete = false;
      naturalWidth = 0;
      private _src = "";
      get src() {
        return this._src;
      }
      set src(value: string) {
        this._src = value;
        requests.push({ src: value, referrerPolicy: this.referrerPolicy });
        queueMicrotask(() => {
          this.complete = true;
          this.naturalWidth = 64;
          this.dispatchEvent(new Event("load"));
        });
      }
    }
    vi.stubGlobal("Image", FakeImage);
    const { container } = render(
      <PersonAvatar personId="u" name="Mira" selection={{ kind: "sign-in" }} pictureUrl={PICTURE} />,
    );
    await waitFor(() => expect(container.querySelector("img")).not.toBeNull());
    const image = container.querySelector("img")!;
    expect(image).toHaveAttribute("src", PICTURE);
    expect(image).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(image).toHaveAttribute("alt", "");
    expect(requests.every((request) => request.src === PICTURE)).toBe(true);
  });

  it("keeps the generated disc when the picture cannot be fetched", async () => {
    class BrokenImage extends EventTarget {
      referrerPolicy = "";
      crossOrigin: string | null = null;
      complete = false;
      naturalWidth = 0;
      set src(_value: string) {
        queueMicrotask(() => this.dispatchEvent(new Event("error")));
      }
    }
    vi.stubGlobal("Image", BrokenImage);
    const { container } = render(
      <PersonAvatar personId="u" name="Mira" selection={{ kind: "sign-in" }} pictureUrl={PICTURE} />,
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container.querySelector("img")).toBeNull();
    expect(container).toHaveTextContent("M");
    expect(discOf(container).background).not.toBe("");
  });
});

describe("choices", () => {
  it("maps each stored word to a selection and back", () => {
    for (const choice of ["auto", "picture", "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"]) {
      expect(choiceFromSelection(selectionFromChoice(choice))).toBe(choice);
    }
    expect(selectionFromChoice("picture")).toEqual({ kind: "sign-in" });
    expect(selectionFromChoice("v8")).toEqual({ kind: "generated", variant: "v8" });
  });

  it.each([undefined, null, "", "v0", "v9", "V1", "https://x.example/p.png", "tide"])(
    "treats %j as automatic",
    (value) => {
      expect(selectionFromChoice(value as string | null | undefined)).toEqual({ kind: "auto" });
    },
  );
});

describe("avatar picker", () => {
  function setup(props: Partial<React.ComponentProps<typeof AvatarPicker>> = {}) {
    const onChange = vi.fn();
    const view = render(
      <AvatarPicker personId="usr_a1b2" name="Mira Chen" onChange={onChange} {...props} />,
    );
    return { onChange, ...view };
  }

  it("offers the eight generated avatars as one radio group with plain names", () => {
    setup();
    const group = screen.getByRole("group", { name: "Choose your avatar" });
    const radios = within(group).getAllByRole("radio");
    expect(radios).toHaveLength(8);
    expect(radios.map((radio) => (radio as HTMLInputElement).value)).toEqual(
      generatedAvatarPalettes.map((palette) => palette.id),
    );
    for (const palette of generatedAvatarPalettes)
      expect(within(group).getByText(palette.label)).toBeVisible();
  });

  it("marks the automatic avatar's swatch as chosen, so the dialog opens on what the person has", () => {
    setup({ value: { kind: "auto" } });
    const automatic = getGeneratedAvatar("usr_a1b2").id;
    const checked = screen.getAllByRole("radio").filter((radio) => (radio as HTMLInputElement).checked);
    expect(checked.map((radio) => (radio as HTMLInputElement).value)).toEqual([automatic]);
  });

  it("reports a chosen variant", async () => {
    const { onChange } = setup({ value: { kind: "auto" } });
    const other = generatedAvatarPalettes.find((palette) => palette.id !== getGeneratedAvatar("usr_a1b2").id)!;
    await userEvent.click(screen.getByRole("radio", { name: new RegExp(other.label) }));
    expect(onChange).toHaveBeenCalledWith({ kind: "generated", variant: other.id });
  });

  it("is operable from the keyboard", async () => {
    const { onChange } = setup({ value: { kind: "generated", variant: "v1" } });
    screen.getAllByRole("radio")[0].focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(onChange).toHaveBeenCalledWith({ kind: "generated", variant: "v2" });
  });

  it("offers the sign-in picture only when the provider shared one", async () => {
    const without = setup();
    expect(screen.queryByRole("radio", { name: /sign-in picture/ })).toBeNull();
    without.unmount();
    const { onChange } = setup({ pictureUrl: PICTURE, value: { kind: "auto" } });
    const picture = screen.getByRole("radio", { name: "Use my sign-in picture" });
    expect(picture).not.toBeChecked();
    await userEvent.click(picture);
    expect(onChange).toHaveBeenCalledWith({ kind: "sign-in" });
  });

  it("does not offer a picture that is not a gateway path", () => {
    setup({ pictureUrl: "https://lh3.example.test/p.png" });
    expect(screen.queryByRole("radio", { name: /sign-in picture/ })).toBeNull();
  });

  it("checks the picture, and no swatch, while it is the choice", () => {
    setup({ pictureUrl: PICTURE, value: { kind: "sign-in" } });
    expect(screen.getByRole("radio", { name: "Use my sign-in picture" })).toBeChecked();
    expect(screen.getAllByRole("radio").filter((radio) => (radio as HTMLInputElement).checked)).toHaveLength(1);
  });

  it("says plainly when the chosen picture is gone and checks the avatar that is shown instead", () => {
    setup({ value: { kind: "sign-in" } });
    expect(screen.getByRole("status")).toHaveTextContent("Your sign-in picture is not available. Your generated avatar is shown.");
    expect(screen.getAllByRole("radio").filter((radio) => (radio as HTMLInputElement).checked)).toHaveLength(1);
  });

  it("locks the choices while saving and says so", () => {
    setup({ pending: true });
    expect(screen.getByRole("group", { name: "Choose your avatar" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Saving…");
  });

  it("shows a failure as an alert, in the caller's words", () => {
    setup({ error: "We couldn't save your avatar. Try again." });
    expect(screen.getByRole("alert")).toHaveTextContent("We couldn't save your avatar. Try again.");
  });

  it("tells the person where the picture comes from and who sees it", () => {
    setup({ pictureUrl: PICTURE });
    expect(
      screen.getByText(/Azizam gets this picture from your sign-in provider\. Only you and the people who run this gateway can see it\./),
    ).toBeVisible();
  });

  it("keeps a swatch's colour when the name changes", () => {
    const first = setup({ name: "Mira" });
    const swatches = (view: ReturnType<typeof render>) =>
      Array.from(view.container.querySelectorAll<HTMLElement>("fieldset label span span span")).map(
        (disc) => disc.style.backgroundImage,
      );
    const before = swatches(first);
    first.unmount();
    const after = setup({ name: "Mira Chen-Okafor" });
    expect(swatches(after)).toEqual(before);
    expect(before.every(Boolean)).toBe(true);
  });
});

describe("person marks", () => {
  it("keeps the shared data-slot and draws from the person id", () => {
    const a = render(<PersonMark name="Mira" personId="usr_9" label="Mira" />);
    expect(screen.getByRole("img", { name: "Mira" })).toHaveAttribute("data-slot", "person-mark");
    const first = discOf(a.container).background;
    a.unmount();
    const b = render(<PersonMark name="Someone Else" personId="usr_9" label="Someone Else" />);
    expect(discOf(b.container).background).toBe(first);
  });

  it("is decorative unless it has a label", () => {
    const { container } = render(<PersonMark name="Mira" personId="usr_9" />);
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
  });

  it("follows the person's chosen avatar", () => {
    const chosen = render(<PersonMark name="Mira" personId="usr_9" avatar={{ choice: "v2" }} />);
    expect(discOf(chosen.container).background).toContain("#294A40");
    chosen.unmount();
    const picture = render(
      <PersonMark name="Mira" personId="usr_9" avatar={{ choice: "picture", picture_url: PICTURE }} />,
    );
    expect(picture.container.firstElementChild).toHaveAttribute("data-avatar-selection", "sign-in");
  });

  it("falls back to the generated avatar for a picture choice with no picture", () => {
    const { container } = render(<PersonMark name="Mira" personId="usr_9" avatar={{ choice: "picture", picture_url: null }} />);
    expect(container.firstElementChild).toHaveAttribute("data-avatar-selection", "generated");
  });

  it("draws every size", () => {
    for (const size of ["2xs", "xs", "sm", "md", "lg"] as const) {
      const { container, unmount } = render(<PersonMark name="Mira" personId="usr_9" size={size} />);
      expect(container.firstElementChild?.className).toMatch(/size-(4|5|6|8|10)\b/);
      unmount();
    }
  });
});

