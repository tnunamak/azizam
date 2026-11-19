"use client";
import { useQueryClient, type Query } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import { isOutageError } from "@/lib/error-copy";

/**
 * One outage, one error, for pages whose sections read with separate queries.
 * A query joins its page by carrying `meta: { page: "<name>" }`. When two or
 * more of those queries have failed with a 5xx or no answer, the gateway is
 * the problem: the page shows one error and one "Try again" (`retryAll`), and
 * each failed section says only that it did not load.
 */
export function usePageOutage(page: string) {
  const client = useQueryClient();
  const cache = client.getQueryCache();
  const failed = (query: Query) =>
    query.meta?.page === page &&
    query.state.status === "error" &&
    isOutageError(query.state.error);
  const read = () => cache.findAll({ predicate: failed }).length;
  const count = useSyncExternalStore(
    (notify) => cache.subscribe(notify),
    read,
    read,
  );
  const fetching = useSyncExternalStore(
    (notify) => cache.subscribe(notify),
    () => cache.findAll({ predicate: (q) => q.meta?.page === page && q.state.fetchStatus === "fetching" }).length > 0,
    () => false,
  );
  return {
    /** Sources that failed with a 5xx or no answer. */
    count,
    /** Two or more sources failed together. */
    outage: count >= 2,
    retrying: fetching,
    /** Ask every failed source again. */
    retryAll: () => void client.refetchQueries({ predicate: failed }),
  };
}
