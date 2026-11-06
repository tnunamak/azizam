import Link from "next/link";
export interface EntityNameProps {
  name?: string | null;
  id?: string;
  href?: string;
  detail?: string;
}
export function EntityName({ name, id, href, detail }: EntityNameProps) {
  const label = name?.trim() || "Unnamed";
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      {href ? (
        <Link prefetch={false}
          href={href}
          className="truncate font-medium underline-offset-4 hover:underline"
        >
          {label}
        </Link>
      ) : (
        <span className="truncate font-medium">{label}</span>
      )}
      {(detail || id) && (
        <span className="text-muted-foreground truncate text-xs">
          {detail || id}
        </span>
      )}
    </span>
  );
}
