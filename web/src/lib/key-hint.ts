/** First characters of a key, enough to tell keys apart and never enough to use one. */
export function keyStart(prefix: string): string {
  return `${prefix.slice(0, 8)}…`;
}
