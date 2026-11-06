"use client";

import { useQuery } from "@tanstack/react-query";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
import { accountApi } from "@/lib/account-api";

/**
 * The person's own access plan: what their Try runs and apps may use. Only
 * the owner changes it, on People, so this section shows it and says so.
 */
export function AccessSection() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["account", "access-plan"],
    queryFn: accountApi.session,
    select: (snapshot) => snapshot.user?.access_plan,
  });
  const plan = data ?? null;
  return (
    <SettingsSection
      id="access"
      title="Access"
      description="What you can use on this gateway. Your owner sets your access plan."
    >
      <SettingRow
        label="Access plan"
        wrapDescription
        description={
          isPending
            ? "Checking your access plan…"
            : isError || data === undefined
              ? "Your access plan could not be confirmed."
              : plan
                ? plan.summary
                : "No access plan is assigned to your account. Ask the owner."
        }
        control={
          plan ? <span className="text-sm font-medium">{plan.name}</span> : undefined
        }
      />
    </SettingsSection>
  );
}
