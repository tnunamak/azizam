import { readdir, readFile, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const output = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../out",
);
const htmlFiles = await findFiles(output, ".html");
const polyfillFiles = new Set();
let removedTags = 0;
const legacyScript = /<script\b(?=[^>]*\bnomodule\b)[^>]*>\s*<\/script>/gi;

for (const htmlFile of htmlFiles) {
  const html = await readFile(htmlFile, "utf8");
  const updated = html.replace(legacyScript, (tag) => {
    const src = tag.match(/\bsrc="([^"]+)"/i)?.[1];
    if (!src) throw new Error(`Legacy script in ${htmlFile} has no source`);
    const url = new URL(src, "https://azizam.invalid");
    if (
      !/^\/ui\/_next\/static\/chunks\/polyfills-[\w-]+\.js$/.test(url.pathname)
    ) {
      throw new Error(`Unexpected legacy script in ${htmlFile}`);
    }
    polyfillFiles.add(
      path.resolve(output, url.pathname.replace(/^\/ui\//, "")),
    );
    removedTags += 1;
    return "";
  });
  if (updated !== html) await writeFile(htmlFile, updated);
}

if (removedTags === 0 || polyfillFiles.size !== 1) {
  throw new Error(
    `Expected one Next.js legacy polyfill per page; found ${removedTags} tags across ${polyfillFiles.size} files`,
  );
}
for (const polyfillFile of polyfillFiles) {
  if (!polyfillFile.startsWith(`${output}${path.sep}`)) {
    throw new Error("Legacy polyfill path escaped the static export");
  }
  await unlink(polyfillFile);
}

console.log(
  `Removed ${removedTags} legacy polyfill tags from the static export.`,
);

async function findFiles(directory, extension) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory())
      files.push(...(await findFiles(fullPath, extension)));
    else if (entry.isFile() && entry.name.endsWith(extension))
      files.push(fullPath);
  }
  return files;
}
