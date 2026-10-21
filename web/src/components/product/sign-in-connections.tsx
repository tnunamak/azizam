"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ErrorState } from "@/components/product/error-state";
import { RelativeTime } from "@/components/product/relative-time";
import { SettingsSection } from "@/components/product/setting-row";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { accountApi } from "@/lib/account-api";
import { toast } from "@/lib/toast";

/**
 * Connections: the apps a person approved by signing in (ChatGPT, Claude.ai),
 * each with Disconnect. Apps & keys and Connect both render this component, so
 * the two pages list and end the same connections. Usage names each one as its
 * own source.
 */
export function SignInConnections({ empty }: { empty: ReactNode }) {
  const queryClient = useQueryClient();
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ["mcp", "connections"],
    queryFn: accountApi.mcpConnections,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => accountApi.revokeMcpConnection(id),
    onSuccess: () => {
      toast.success("Disconnected. That app can't use your account any more.");
      void queryClient.invalidateQueries({ queryKey: ["mcp", "connections"] });
    },
    onError: () => toast.error("Couldn't disconnect. Try again."),
  });
  return (
    <SettingsSection
      title="Connected with sign-in"
      description="Apps you approved by signing in. Disconnecting stops them at once."
    >
      <div className="p-4">
        {isPending ? (
          <Skeleton className="h-10 w-full" />
        ) : isError ? (
          <ErrorState
            message="Couldn't load your connections."
            onRetry={() => void refetch()}
          />
        ) : data.length === 0 ? (
          <p className="text-muted-foreground text-meta">{empty}</p>
        ) : (
          <ul aria-label="Connections" className="flex flex-col gap-3">
            {data.map((connection) => (
              <li
                key={connection.id}
                className="flex flex-wrap items-center justify-between gap-2"
              >
                <div className="min-w-0">
                  <p className="text-body font-medium break-words">
                    {connection.client_name}
                  </p>
                  <p className="text-muted-foreground text-meta">
                    Returns to {connection.redirect_host} · connected{" "}
                    <RelativeTime ts={connection.created_at} /> · last used{" "}
                    <RelativeTime
                      ts={connection.last_used_at}
                      fallback="never"
                    />
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(connection.id)}
                  aria-label={`Disconnect ${connection.client_name}`}
                >
                  Disconnect
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </SettingsSection>
  );
}
