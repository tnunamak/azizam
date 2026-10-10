"use client";

import { useSyncExternalStore, type ReactNode } from "react";
import type { ExternalToast } from "sonner";

type ToastMessage = ReactNode;
type ToastMethod = "success" | "error" | "info" | "warning" | "loading";

interface PendingToast {
  id: string | number;
  method: ToastMethod;
  message: ToastMessage;
  options?: ExternalToast;
}

const listeners = new Set<() => void>();
const pending: PendingToast[] = [];
let requested = false;
let ready = false;
let flushing = false;
let nextId = 0;

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getRequested() {
  return requested;
}

export function useToastRequested() {
  return useSyncExternalStore(subscribe, getRequested, () => false);
}

function requestToaster() {
  if (requested) return;
  requested = true;
  for (const listener of listeners) listener();
}

export function markToasterReady() {
  ready = true;
  flush();
}

function flush() {
  if (!ready || flushing || pending.length === 0) return;
  flushing = true;
  void import("sonner")
    .then(({ toast: sonnerToast }) => {
      while (pending.length > 0) {
        const item = pending.shift()!;
        sonnerToast[item.method](item.message, {
          ...item.options,
          id: item.id,
        });
      }
    })
    .catch(() => {
      pending.length = 0;
    })
    .finally(() => {
      flushing = false;
      flush();
    });
}

function enqueue(
  method: ToastMethod,
  message: ToastMessage,
  options?: ExternalToast,
) {
  requestToaster();
  const id = options?.id ?? `azizam-toast-${++nextId}`;
  pending.push({ id, method, message, options });
  flush();
  return id;
}

export const toast = {
  success: (message: ToastMessage, options?: ExternalToast) =>
    enqueue("success", message, options),
  error: (message: ToastMessage, options?: ExternalToast) =>
    enqueue("error", message, options),
  info: (message: ToastMessage, options?: ExternalToast) =>
    enqueue("info", message, options),
  warning: (message: ToastMessage, options?: ExternalToast) =>
    enqueue("warning", message, options),
  loading: (message: ToastMessage, options?: ExternalToast) =>
    enqueue("loading", message, options),
};
