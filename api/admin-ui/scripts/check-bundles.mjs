// Check the built documents against the Content-Security-Policy they are
// served under (script-src 'self', style-src 'self'): no inline script or
// style, and every script and stylesheet a file of this build under
// ./assets/. Also that the registration build contains every font file the
// server-rendered /web stylesheet names (web-fonts.json). Runs at the end of `npm run build`, so the image and CI both
// fail on a document the policy would break, rather than a browser later.
//
// Plain Node, no dependencies, so the build needs nothing it did not already.

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Every way *html* breaks the policy; `exists` answers for paths relative to the document. */
export function offences(html, exists) {
  const found = [];
  for (const tag of html.match(/<script\b[^>]*>/gi) ?? []) {
    if (!/\bsrc\s*=/i.test(tag)) found.push("an inline <script> (no src), which script-src 'self' refuses");
  }
  const references = [
    ...[...html.matchAll(/<script\b[^>]*\bsrc\s*=\s*"([^"]*)"/gi)].map((m) => m[1]),
    ...[...html.matchAll(/<link\b[^>]*\bhref\s*=\s*"([^"]*)"/gi)].map((m) => m[1]),
  ];
  for (const reference of references) {
    if (!reference.startsWith("./assets/")) found.push(`${reference} is outside ./assets/`);
    else if (!exists(reference.slice(2))) found.push(`${reference} is not in the build`);
  }
  if (/<[a-z][^>]*\sstyle\s*=/i.test(html)) found.push("a style attribute, which style-src 'self' refuses");
  if (/<style\b/i.test(html)) found.push("an inline <style>, which style-src 'self' refuses");
  return found;
}

/** Every name in *fonts* that `exists` does not find in the registration assets. */
export function missingFonts(fonts, exists) {
  return fonts
    .filter((name) => !exists(name))
    .map((name) => `${name} is named by the /web stylesheet but is not in the registration build`);
}

const DOCUMENTS = ["dist/index.html", "dist/registration/index.html"];

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..");
  let failed = false;
  for (const document of DOCUMENTS) {
    const path = join(root, document);
    if (!existsSync(path)) {
      console.error(`${document}: missing`);
      failed = true;
      continue;
    }
    const found = offences(readFileSync(path, "utf8"), (relative) => existsSync(join(dirname(path), relative)));
    for (const offence of found) console.error(`${document}: ${offence}`);
    failed ||= found.length > 0;
  }
  const { files: fonts } = JSON.parse(readFileSync(join(root, "web-fonts.json"), "utf8"));
  const missing = missingFonts(fonts, (name) => existsSync(join(root, "dist/registration/assets", name)));
  for (const offence of missing) console.error(`web-fonts.json: ${offence}`);
  failed ||= missing.length > 0;
  if (failed) process.exit(1);
  console.log(`checked ${DOCUMENTS.join(", ")} against the pages' Content-Security-Policy, and the /web fonts`);
}
