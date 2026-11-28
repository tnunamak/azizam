"use client";

import { useQuery } from "@tanstack/react-query";

import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { adminFetch, type AccessPlan, type App, type ConnectionStatus } from "@/lib/api";
import { disconnectImpact, impactSentence } from "./subscription-access";
import { subscriptionLabel } from "./subscription-state";

/**
 * Disconnect one subscription. The counts come from the access-plans and apps
 * reads; if either read fails the dialog says so instead of guessing.
 */
export function SubscriptionDisconnectDialog({
  open,
  onOpenChange,
  sub,
  onDisconnected,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sub: ConnectionStatus;
  onDisconnected: (view: ConnectionStatus) => void;
}) {
  const noun = subscriptionLabel(sub);
  const name = sub.display_name || sub.kind;
  const plans = useQuery({
    queryKey: ["subscriptions", "impact", "access-plans"],
    queryFn: () => adminFetch<{ access_plans: AccessPlan[] }>("/admin/api/access-plans"),
    enabled: open,
    retry: false,
  });
  const apps = useQuery({
    queryKey: ["subscriptions", "impact", "apps"],
    queryFn: () => adminFetch<{ apps: App[] }>("/admin/api/apps"),
    enabled: open,
    retry: false,
  });
  const failed = plans.isError || apps.isError;
  const ready = plans.data && apps.data;
  const impact = ready ? disconnectImpact(sub.connection_id, plans.data!.access_plans, apps.data!.apps) : null;
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Disconnect ${noun}?`}
      confirmLabel="Disconnect"
      typedName={noun}
      description={
        <div className="space-y-3">
          <p>
            {impact
              ? impactSentence(impact)
              : failed
                ? "Could not check which access plans and apps use it."
                : "Checking who uses it…"}
          </p>
          <ul className="list-disc space-y-1 pl-5">
            <li>Requests stop using this subscription right away. A model can still run on a paid route only when its access plan allows paid fallback.</li>
            {sub.revokes_sign_in === false ? (
              <li>
                The sign-in stays on the computer running Azizam.
              </li>
            ) : (
              <li>
                We also tell {name} to end this sign-in. If it can&apos;t be reached, end it in {name}&apos;s settings.
              </li>
            )}
            <li>The model list stays, so reconnecting needs no new grants.</li>
          </ul>
        </div>
      }
      onConfirm={async () => {
        const answer = await adminFetch<{ subscription: ConnectionStatus }>(
          `/admin/api/subscriptions/${encodeURIComponent(sub.kind)}`,
          { method: "DELETE" },
        );
        onDisconnected(answer.subscription);
      }}
    />
  );
}
