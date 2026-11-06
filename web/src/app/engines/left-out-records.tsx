"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { api, ApiError, type LeftOutRecord, type ServiceRegistry } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { Callout } from "@/components/product/status-ui";
import { toast } from "@/lib/toast";

const SECTION_WORDS: Record<string, string> = {
  services: "Service",
  protocol_frontends: "Frontend",
  workflows: "Workflow",
  capabilities: "Capability",
};

function reason(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status === 412
      ? "The registry changed elsewhere. Close this and try again."
      : error.message;
  }
  return "We could not save this record. Try again.";
}

/** The records the registry leaves out, with their error, an editor for each and a remove. */
export function LeftOutRecords({
  records,
  onChanged,
}: {
  records: readonly LeftOutRecord[];
  onChanged: (registry: ServiceRegistry) => void;
}) {
  const [editing, setEditing] = useState<LeftOutRecord | null>(null);
  const [removing, setRemoving] = useState<LeftOutRecord | null>(null);

  const remove = useMutation({
    mutationFn: (item: LeftOutRecord) => api.services.removeLeftOut(item.section, item.id),
    onSuccess: (registry) => {
      onChanged(registry);
      toast.success("Record removed");
    },
    onError: (error) => toast.error("Record could not be removed", { description: reason(error) }),
  });

  if (records.length === 0) return null;
  return (
    <section aria-label="Left out" className="flex flex-col gap-3">
      <Callout
        as="div"
        severity="attention"
        title="Some stored records are left out"
        meta="They do not validate, so the gateway does not use them. Edit one to fix it, or remove it. Nothing else is affected."
      />
      <ul className="bg-card rounded-panel divide-y overflow-hidden border">
        {records.map((item) => (
          <li key={`${item.section}:${item.id}`} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
            <div className="min-w-0 flex-1">
              <p className="flex items-center gap-2">
                <span className="text-body text-foreground font-medium break-all">{item.id}</span>
                <span className="text-meta text-muted-foreground">{SECTION_WORDS[item.section] ?? item.section}</span>
              </p>
              <p className="text-meta text-muted-foreground mt-0.5 break-words">{item.error}</p>
            </div>
            <div className="flex shrink-0 gap-2">
              <Button type="button" size="sm" variant="outline" aria-label={`Edit ${item.id}`} onClick={() => setEditing(item)}>
                Edit
              </Button>
              <Button type="button" size="sm" variant="outline" aria-label={`Remove ${item.id}`} onClick={() => setRemoving(item)}>
                Remove
              </Button>
            </div>
          </li>
        ))}
      </ul>
      <ConfirmDialog
        open={Boolean(removing)}
        title="Remove record?"
        description={removing ? `Remove ${removing.id} from the stored registry? The gateway is not using it now.` : ""}
        confirmLabel="Remove record"
        danger
        typedName={removing?.id}
        onOpenChange={(open) => !open && setRemoving(null)}
        onConfirm={() => {
          if (!removing) return;
          return remove.mutateAsync(removing).then(() => undefined);
        }}
      />
      {editing && (
        <LeftOutEditor
          item={editing}
          onClose={() => setEditing(null)}
          onSaved={(registry) => {
            onChanged(registry);
            setEditing(null);
            toast.success("Record saved");
          }}
        />
      )}
    </section>
  );
}

function LeftOutEditor({
  item,
  onClose,
  onSaved,
}: {
  item: LeftOutRecord;
  onClose: () => void;
  onSaved: (registry: ServiceRegistry) => void;
}) {
  const [text, setText] = useState(() => (item.parsed ? JSON.stringify(item.record ?? {}, null, 2) : "{}"));
  const [problem, setProblem] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (record: unknown) => api.services.fixLeftOut(item.section, item.id, record),
    onSuccess: onSaved,
    onError: (error) => setProblem(reason(error)),
  });

  function submit() {
    let record: unknown;
    try {
      record = JSON.parse(text);
    } catch {
      setProblem("This is not valid JSON. Check the brackets, quotes and commas.");
      return;
    }
    setProblem(null);
    save.mutate(record);
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !save.isPending && onClose()}>
      <DialogContent className="flex! max-h-[85dvh] flex-col gap-4! overflow-y-auto sm:max-w-2xl">
        <DialogHeader className="text-left">
          <DialogTitle>Edit {item.id}</DialogTitle>
          <DialogDescription>{item.error}</DialogDescription>
        </DialogHeader>
        {item.parsed ? (
          <p className="text-meta text-muted-foreground">
            Secrets are not shown. Each one is a secret entry with an id and a configured flag; leave the entries as
            they are and they keep their value. Change a secret with the secret entry controls once the record works.
          </p>
        ) : (
          <div className="text-meta text-muted-foreground space-y-1">
            <p>This record cannot be shown: it is not in a shape the gateway can read, so none of its values are displayed.</p>
            {(item.failed_fields ?? []).length > 0 && (
              <p>
                Fields that could not be read:{" "}
                {(item.failed_fields ?? []).map((field) => (
                  <code key={field} className="mr-2">{field}</code>
                ))}
              </p>
            )}
            {item.secret_slots.length > 0 && (
              <p>
                Secret entries it holds:{" "}
                {item.secret_slots.map((slot) => (
                  <code key={slot} className="mr-2">{slot}</code>
                ))}
              </p>
            )}
            <p>Type a corrected record to replace it.</p>
          </div>
        )}
        <label className="text-label flex flex-col gap-1.5">
          Record
          <Textarea
            aria-label="Record"
            className="min-h-64 font-mono text-xs"
            spellCheck={false}
            value={text}
            onChange={(event) => setText(event.target.value)}
            disabled={save.isPending}
          />
        </label>
        {problem && (
          <p role="alert" className="text-destructive text-meta break-words">
            {problem}
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="button" onClick={submit} disabled={save.isPending}>
            Save record
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
