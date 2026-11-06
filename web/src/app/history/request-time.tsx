import { formatTimestamp } from "@/lib/format";

/** The exact time, in words, for a title: "Oct 23, 2026, 11:41 AM". */
function exactTimeWords(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** When a request was made: "2 minutes ago", and the exact time on hover and in the title. */
export function RequestTime({ at, className }: { at: number; className?: string }) {
  const stamp = formatTimestamp(at);
  return (
    <time dateTime={stamp.dateTime} title={stamp.dateTime ? exactTimeWords(at) : undefined} className={className}>
      {stamp.children}
    </time>
  );
}
