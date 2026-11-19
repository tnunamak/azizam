"use client";

import { useId } from "react";
import { Checkbox } from "@/components/ui/checkbox";
import type { RemotePlan } from "@/lib/engines-api";
import { Note } from "../parts/form";

/** How Azizam will talk to a server on another computer, in the owner's words. */
const HOW: Record<RemotePlan["transport"], string> = {
  tls: "over https",
  private_plaintext: "with plain http: anyone on that network can read what Azizam and the server send each other",
};

/** The reason every save stays off while a plain http network has not been trusted. */
export const TRUST_REASON = "Tick “I trust this network” to add this server. It is reached with plain http.";

/** True while the owner has been asked to trust the network and has not said yes. */
export function trustPending(remote: RemotePlan | undefined, trusted: boolean): boolean {
  return Boolean(remote?.trust_needed) && !trusted;
}

/**
 * The network the owner is asked to trust, shown with the yes beside it. The network is computed by the gateway from the
 * address; the owner never types it.
 */
export function NetworkTrust({
  remote,
  label,
  trusted,
  onTrust,
  error,
}: {
  remote: RemotePlan;
  label: string;
  trusted: boolean;
  onTrust: (on: boolean) => void;
  error?: string | null;
}) {
  const id = useId();
  return (
    <div role="group" aria-label="The network you are asked to trust" className="bg-card rounded-panel flex flex-col gap-3 border p-4">
      <p className="text-body">
        <b className="font-medium">{`Azizam will call ${remote.network} on your network`}</b>
      </p>
      <p className="text-meta text-muted-foreground">{HOW[remote.transport]}</p>
      {remote.trust_needed && (
        <div className="flex items-center gap-2">
          <Checkbox
            id={id}
            checked={trusted}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? `${id}-error` : undefined}
            onCheckedChange={(on) => onTrust(on === true)}
          />
          <label htmlFor={id} className="text-meta text-foreground font-medium">
            {label}
          </label>
        </div>
      )}
      {error && (
        <p id={`${id}-error`} role="alert" className="text-meta text-danger">
          {error}
        </p>
      )}
      {trustPending(remote, trusted) && !error && <Note tone="warn">{TRUST_REASON}</Note>}
    </div>
  );
}
