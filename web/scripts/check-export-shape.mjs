// The gateway serves only the export shapes it knows (gateway/ui_routes.py).
// Fail the build on any other file, so a Next upgrade that emits a new kind
// of file breaks here instead of answering 404 in production.
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const output = path.resolve(here, "../out");
const registry = JSON.parse(
  await readFile(path.resolve(here, "../../gateway/ui_pages.json"), "utf8"),
);
const pages = Object.keys(registry.pages);

const allowed = new Set([
  "index.html",
  "index.txt",
  "404.html",
  "404/index.html",
  ".azizam-build-complete",
]);
for (const name of pages.filter(Boolean)) {
  allowed.add(`${name}/index.html`);
  allowed.add(`${name}/index.txt`);
}

const files = [];
for (const entry of await readdir(output, {
  recursive: true,
  withFileTypes: true,
})) {
  if (entry.isFile())
    files.push(path.relative(output, path.join(entry.parentPath, entry.name)));
}
const unexpected = files.filter(
  (file) => !allowed.has(file) && !file.startsWith("_next/static/"),
);
const missing = pages
  .flatMap((name) => [
    `${name ? `${name}/` : ""}index.html`,
    `${name ? `${name}/` : ""}index.txt`,
  ])
  .filter((file) => !files.includes(file));
if (unexpected.length || missing.length) {
  for (const file of unexpected)
    console.error(`unexpected export file: ${file}`);
  for (const file of missing) console.error(`missing page file: ${file}`);
  process.exit(1);
}
console.log(`export shape: ${files.length} files, ${pages.length} pages`);
