import { splitSecretPrefix } from "@/lib/format";
import { cn } from "@/lib/cn";

export interface SecretMaskProps {
  /** Key prefix from the API (e.g. "opk_AbC123…"), never full key material. */
  prefix: string;
  className?: string;
}

/** Presentation law #3: no raw secrets/prefixes. Renders scheme + dots +
 * last four: `opk_ •••• ab12`. */
export function SecretMask({ prefix, className }: SecretMaskProps) {
  const { scheme, last4 } = splitSecretPrefix(prefix);
  return (
    <span
      className={cn(
        "text-vf-micro text-vf-muted inline-flex items-baseline gap-1 font-mono",
        className,
      )}
    >
      {scheme && <span>{scheme}</span>}
      <span aria-hidden="true">••••</span>
      <span>{last4}</span>
    </span>
  );
}
