"use client";

import { useQuery } from "@tanstack/react-query";
import { protocolDescriptionsApi } from "@/lib/protocol-descriptions-api";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { FormField, NativeSelect } from "./form";

export interface ProtocolDraft {
  dispatch: "" | "protocol";
  protocol: string;
}

/** Select installed, validated data. No field accepts an uninstalled description id. */
export function ProtocolChoice({
  value,
  onChange,
  disabled = false,
  tasks,
}: {
  value: ProtocolDraft;
  onChange: (value: ProtocolDraft) => void;
  disabled?: boolean;
  /** When given, the operations the chosen description declares are offered as a pick list of tasks. Never typed. */
  tasks?: { picked: string[]; onChange: (picked: string[]) => void };
}) {
  const { data: catalog, isLoading, isError, refetch } = useQuery({
    queryKey: ["engineProtocolDescriptions"],
    queryFn: protocolDescriptionsApi.list,
    staleTime: 0,
    enabled: value.dispatch === "protocol",
  });
  const selected = catalog?.protocols.find(
    (row) => row.id === value.protocol,
  );
  const unavailable = Boolean(
    value.protocol &&
    catalog &&
    (!selected || !selected.dispatch_supported),
  );
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <FormField
        label="Request dispatch"
        help="Protocol descriptions are an opt-in for local loopback engines. They do not enforce paid-provider access or spend limits."
      >
        {(control) => (
          <NativeSelect
            {...control}
            value={value.dispatch}
            disabled={disabled}
            onChange={(event) =>
              onChange({
                ...value,
                dispatch: event.target.value as ProtocolDraft["dispatch"],
              })
            }
          >
            <option value="">Existing gateway route</option>
            <option value="protocol">
              Use an installed protocol description
            </option>
          </NativeSelect>
        )}
      </FormField>
      {value.dispatch === "protocol" && (
        <>
          {isLoading && (
            <p role="status" className="text-meta text-muted-foreground">
              Loading protocol descriptions…
            </p>
          )}
          {isError && (
            <div role="alert" className="flex flex-col gap-2">
              <p>Protocol descriptions could not be loaded.</p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void refetch()}
              >
                Retry protocol descriptions
              </Button>
            </div>
          )}
          {catalog && (
            <>
              <FormField
                label="Protocol description"
                required
                help="Choose a validated description installed in the registry or your persistent protocols directory."
                error={
                  unavailable
                    ? "The saved description is unavailable. Install a valid copy or choose another description."
                    : !value.protocol
                      ? "Choose an installed protocol description."
                      : undefined
                }
                after={
                  selected && (
                    <div
                      role="status"
                      className="text-meta flex flex-col gap-1"
                    >
                      <p>{selected.validation.message}</p>
                      <p className="text-muted-foreground">
                        {selected.source === "user"
                          ? "Installed by you"
                          : "Bundled with Azizam"}{" "}
                        · {selected.executor} · {selected.operations.join(", ")}
                      </p>
                    </div>
                  )
                }
              >
                {(control) => (
                  <NativeSelect
                    {...control}
                    value={value.protocol}
                    disabled={disabled}
                    onChange={(event) =>
                      onChange({ ...value, protocol: event.target.value })
                    }
                  >
                    <option value="">Choose a description</option>
                    {unavailable && !selected && (
                      <option value={value.protocol} disabled>
                        {value.protocol} (unavailable)
                      </option>
                    )}
                    {catalog.protocols.map((row) => (
                      <option
                        key={row.id}
                        value={row.id}
                        disabled={!row.dispatch_supported}
                      >
                        {row.title} ({row.id})
                        {!row.dispatch_supported
                          ? " · unavailable for dispatch"
                          : ""}
                      </option>
                    ))}
                  </NativeSelect>
                )}
              </FormField>
              {tasks && selected?.dispatch_supported && selected.operations.length > 0 && (
                <fieldset className="flex flex-col gap-2">
                  <legend className="text-body font-medium">Tasks this engine does</legend>
                  <p className="text-meta text-muted-foreground">
                    The description declares these operations. Tick the ones this engine offers.
                  </p>
                  {selected.operations.map((operation) => (
                    <label key={operation} className="text-body flex min-h-8 cursor-pointer items-center gap-3 max-sm:min-h-11">
                      <Checkbox
                        aria-label={operation}
                        checked={tasks.picked.includes(operation)}
                        disabled={disabled}
                        onCheckedChange={(on) =>
                          tasks.onChange(on === true ? [...tasks.picked, operation] : tasks.picked.filter((task) => task !== operation))
                        }
                      />
                      <span>{operation}</span>
                    </label>
                  ))}
                </fieldset>
              )}
              {catalog.protocols.length === 0 && (
                <p role="status">
                  No valid protocol descriptions are installed.
                </p>
              )}
              {catalog.problems.map((problem) => (
                <p key={problem.file} className="text-meta text-warning">
                  {problem.file}: {problem.reason}
                </p>
              ))}
            </>
          )}
        </>
      )}
    </div>
  );
}
