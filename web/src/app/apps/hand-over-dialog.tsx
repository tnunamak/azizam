"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { peopleApi } from "@/app/people/people-api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useViewer } from "@/components/shell/session";
import { api } from "@/lib/api";

/** The service app a handover is about, with the plan it has today. */
export interface HandOverApp {
  id: string;
  name: string;
  /** The person it answers to now, left out of the choices. */
  currentPersonId?: string | null;
  /** "Same as its person": the app takes whatever plan its new person has. */
  sameAsPerson: boolean;
  planName: string | null;
}

/**
 * Owner only: change who a service app answers to. The app is bounded by its person's plan, so the dialog says
 * which plan it will run under before the move. The move stops the app's keys and trusted addresses at once: each
 * key was issued by the person it answered to, so the new person makes a new one.
 */
export function HandOverDialog({
  app,
  onClose,
  onDone,
}: {
  app: HandOverApp;
  onClose: () => void;
  onDone: () => void | Promise<void>;
}) {
  const [personId, setPersonId] = useState("");
  const { data, isPending, isError } = useQuery({
    queryKey: ["people", "answers-to"],
    queryFn: peopleApi.people,
  });
  // The counts come from the owner's app list, so the confirmation can say exactly what stops.
  const { data: adminApps } = useQuery({ queryKey: ["admin", "apps"], queryFn: api.apps.list });
  const counts = adminApps?.find((candidate) => candidate.id === app.id);
  const choices = (data?.people ?? []).filter(
    (person) => !person.disabled && person.id !== app.currentPersonId,
  );
  const chosen = choices.find((person) => person.id === personId);
  const viewerId = useViewer().id;
  const isViewer = !!chosen && !!viewerId && chosen.id === viewerId;
  // The person's display name, or their email only when they have no name; "you" when it is the viewer.
  const chosenName = chosen ? (chosen.name || chosen.email || "this person") : "";
  const mutation = useMutation({
    mutationFn: () => api.apps.handOver(app.id, personId),
    onSuccess: async () => {
      await onDone();
      onClose();
    },
  });
  const possessive = isViewer ? "your" : `${chosenName}'s`;
  const preview = !chosen
    ? null
    : !chosen.plan_name
      ? `${isViewer ? "You have" : `${chosenName} has`} no access plan, so ${app.name} would stop working until ${isViewer ? "you get" : "they get"} one.`
      : app.sameAsPerson
        ? `${app.name} will run under ${possessive} plan: ${chosen.plan_name}.`
        : `${app.name} keeps its plan, ${app.planName ?? "its own plan"}, but never gets more than ${possessive} plan: ${chosen.plan_name}.`;
  // Say only what applies: the keys that stop and the trusted addresses that go. Nothing to stop, nothing said.
  const keys = counts?.live_keys ?? 0;
  const addresses = counts?.trusted_addresses ?? 0;
  const keysPart = keys === 1 ? "Its key stops now" : `Its ${keys} keys stop now`;
  const addressesPart = addresses === 1 ? "its trusted address is removed" : `its ${addresses} trusted addresses are removed`;
  const stopsSentence = !counts
    ? "Its keys and trusted addresses stop now."
    : keys && addresses
      ? `${keysPart} and ${addressesPart}.`
      : keys
        ? `${keysPart}.`
        : addresses
          ? `${addressesPart[0].toUpperCase()}${addressesPart.slice(1)}.`
          : "";
  const makesKey = isViewer
    ? "You make a new key on Apps & keys."
    : `${chosen ? chosenName : "The new person"} makes a new key on Apps & keys.`;
  const stops = [stopsSentence, makesKey].filter(Boolean).join(" ");
  return (
    <Dialog open onOpenChange={(open) => !open && !mutation.isPending && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Who does {app.name} answer to?</DialogTitle>
          <DialogDescription>
            The app keeps its name, plan choice and usage history, and answers to the person you pick.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <label htmlFor="handover-person" className="text-label">
            Answers to
          </label>
          <Select value={personId} onValueChange={setPersonId} disabled={mutation.isPending}>
            <SelectTrigger id="handover-person" aria-label="Answers to" className="w-full">
              <SelectValue placeholder={isPending ? "Loading people…" : "Choose a person"} />
            </SelectTrigger>
            <SelectContent>
              {choices.map((person) => (
                <SelectItem key={person.id} value={person.id}>
                  {person.name || person.email || person.id}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {isError ? (
            <p role="alert" className="text-destructive text-meta">
              The people could not be loaded. Try again.
            </p>
          ) : null}
          {preview ? <p className="text-muted-foreground text-meta">{preview}</p> : null}
        </div>
        <p className="text-meta" data-testid="handover-stops">
          {stops}
        </p>
        {mutation.isError ? (
          <p role="alert" className="text-destructive text-meta">
            {mutation.error instanceof Error ? mutation.error.message : "The app could not be moved. Try again."}
          </p>
        ) : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button disabled={!chosen || mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Moving…" : "Move app"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
