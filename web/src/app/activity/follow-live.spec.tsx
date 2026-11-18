import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FollowLive, useFollowLive } from "@/app/activity/follow-live";

afterEach(cleanup);

describe("FollowLive", () => {
  it("starts with Follow live and aria-pressed false", () => {
    render(<FollowLive following={false} onChange={vi.fn()} updatedAt={null} />);
    expect(screen.getByRole("button", { name: "Follow live" }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("requests following when the paused button is clicked", () => {
    const onChange = vi.fn();
    render(<FollowLive following={false} onChange={onChange} updatedAt={null} />);
    fireEvent.click(screen.getByRole("button", { name: "Follow live" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith(true);
  });

  it("shows Pause while following and requests pausing on click", () => {
    const onChange = vi.fn();
    render(<FollowLive following onChange={onChange} updatedAt={null} />);
    const button = screen.getByRole("button", { name: "Pause" });
    expect(button.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(button);
    expect(onChange).toHaveBeenCalledExactlyOnceWith(false);
  });

  it("omits Updated when updatedAt is null", () => {
    render(<FollowLive following={false} onChange={vi.fn()} updatedAt={null} />);
    expect(screen.queryByText(/Updated/)).toBeNull();
  });

  it("shows Updated with the shared relative time for a number", () => {
    const updatedAt = Date.parse("2026-11-18T12:00:00Z");
    render(<FollowLive following={false} onChange={vi.fn()} updatedAt={updatedAt} />);
    const time = screen.getByText(/^Updated/).querySelector("time");
    expect(time?.getAttribute("datetime")).toBe("2026-11-18T12:00:00.000Z");
    expect(time?.textContent).toBeTruthy();
  });
});

describe("useFollowLive", () => {
  it("starts false and returns a working setter", () => {
    function Probe() {
      const [following, setFollowing] = useFollowLive();
      return <button onClick={() => setFollowing(true)}>{String(following)}</button>;
    }
    render(<Probe />);
    fireEvent.click(screen.getByRole("button", { name: "false" }));
    expect(screen.getByRole("button", { name: "true" })).toBeTruthy();
  });
});
