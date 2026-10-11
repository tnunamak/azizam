import Link from "next/link";

export function serviceUnavailable(name: string) {
  const words = name.replace(/[_-]+/g, " ");
  return `${words.charAt(0).toUpperCase()}${words.slice(1)} can’t take requests right now.`;
}

export function ServiceNotice({
  message,
  section = "engines",
}: {
  message: string;
  section?: string;
}) {
  return (
    <p role="status" className="text-sm">
      {message}{" "}
      <Link
        className="text-link underline underline-offset-4"
        href={`/status#${section.toLowerCase()}`}
      >
        See Status
      </Link>
    </p>
  );
}
