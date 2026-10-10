"use client";

import { useQuery } from "@tanstack/react-query";
import { productApi } from "@/lib/product-api";
import { modelDisplayName, type CapabilityNameRow } from "@/lib/model-name";

/**
 * The name a person reads for a model id on any screen: the models catalog's
 * title, else the name of the capability that answers to it, else the id made
 * readable. A weight file's name ("…-Q4_K_M.gguf") is never shown, not even
 * in a tooltip.
 */
export function useModelName(): (id: string) => string {
  const { data: catalog } = useQuery({
    queryKey: ["models", "catalog"],
    queryFn: productApi.models,
    retry: false,
  });
  const { data: home } = useQuery({
    queryKey: ["account", "home"],
    queryFn: productApi.home,
    retry: false,
  });
  const capabilities: CapabilityNameRow[] = Array.isArray(home?.capabilities)
    ? home.capabilities
    : [];
  return (id: string) => modelDisplayName(id, catalog, capabilities);
}
