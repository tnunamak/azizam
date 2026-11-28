"use client";

import type { ReactNode } from "react";

import { ListRow } from "@/components/product/list-card";
import { StateWord } from "@/components/product/status-ui";
import { Button } from "@/components/ui/button";
import type { ConnectionStatus } from "@/lib/api";
import {
  subscriptionLabel,
  subscriptionRowState,
  type SubscriptionAction,
} from "./subscription-state";

const ACTION_LABEL: Record<SubscriptionAction, string> = {
  connect: "Connect",
  reconnect: "Reconnect",
  open: "Open",
  check: "Check now",
};

function setupNote(sub: ConnectionStatus): string {
  if (sub.status !== "not_connected") return "";
  const runner = sub.runner;
  switch (runner?.state) {
    case "missing": return "Setup is needed before you connect.";
    case "installing": {
      const percent = runner.percent;
      return typeof percent === "number" && Number.isInteger(percent) && percent >= 0 && percent <= 100
        ? `Setting up… ${percent}%`
        : "Setting up…";
    }
    case "failed": return "Setup didn't finish. Choose Connect to try again.";
    default: return "";
  }
}

/** One subscription kind: its name, one state word, one detail line, one action. */
export function SubscriptionRow({
  sub,
  onAction,
  menu,
  busy = false,
  expanded = false,
  now,
}: {
  sub: ConnectionStatus;
  onAction: (action: SubscriptionAction) => void;
  /** The overflow menu (Disconnect lives there). */
  menu?: ReactNode;
  busy?: boolean;
  expanded?: boolean;
  now?: number;
}) {
  const state = subscriptionRowState(sub, now);
  const name = subscriptionLabel(sub);
  const actionLabel = state.action === "open" && expanded ? "Close" : ACTION_LABEL[state.action];
  return (
    <ListRow
      lines={2}
      title={name}
      meta={state.detail}
      metaTitle={state.detail}
      facts={[<StateWord key="state" tone={state.tone} word={state.word} />]}
      factClassName="w-28"
      trailing={
        <span className="flex items-center gap-1">
          <span role="status" className="max-w-44 text-xs text-muted-foreground empty:sr-only">{setupNote(sub)}</span>
          <Button
            size="sm"
            variant={state.action === "reconnect" || state.action === "connect" ? "default" : "outline"}
            disabled={busy}
            aria-expanded={state.action === "open" ? expanded : undefined}
            aria-label={`${actionLabel} ${name}`}
            onClick={() => onAction(state.action)}
          >
            {actionLabel}
          </Button>
          {menu}
        </span>
      }
    />
  );
}
