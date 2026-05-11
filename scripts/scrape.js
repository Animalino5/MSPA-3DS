const fs = require("fs");

console.log("STARTING SCRAPER");

const visited = new Set();
const moduleCache = {};

// =====================================
// Fetch asset index once
// =====================================

let assetIndex = null;

async function loadAssetIndex() {

  if (assetIndex) {
    return assetIndex;
  }

  console.log("Fetching asset index...");

  const res = await fetch(
    "https://homestuck.com/assets/index-D7c7xGSX.js"
  );

  if (!res.ok) {
    throw new Error(
      `Failed asset index fetch: ${res.status}`
    );
  }

  assetIndex = await res.text();

  console.log("Loaded asset index.");

  return assetIndex;
}

// =====================================
// Discover page JS module
// =====================================

async function findModule(pageNum) {

  const padded =
    String(pageNum).padStart(6, "0");

  // Cached already?
  if (moduleCache[padded]) {
    return moduleCache[padded];
  }

  const indexJs = await loadAssetIndex();

  // Find all matching modules
  const regex = new RegExp(
    `${padded}HS-[A-Za-z0-9_-]+\\.js`,
    "g"
  );

  const matches = [
    ...new Set(indexJs.match(regex) || [])
  ];

  if (matches.length === 0) {

    console.log(
      `No module found for ${padded}`
    );

    return null;
  }

  // Usually only one exists
  const jsFile = matches[0];

  moduleCache[padded] = jsFile;

  return jsFile;
}

// =====================================
// Scrape page
// =====================================

async function scrapePage(pageNum) {

  if (visited.has(pageNum)) {
    return;
  }

  visited.add(pageNum);

  const padded =
    String(pageNum).padStart(6, "0");

  console.log(`\n=== PAGE ${padded} ===`);

  // =====================================
  // Find module
  // =====================================

  const jsFile = await findModule(pageNum);

  if (!jsFile) {

    console.log(
      `Skipping ${padded}`
    );

    return;
  }

  const jsUrl =
    `https://homestuck.com/assets/${jsFile}`;

  console.log(`Fetching JS: ${jsFile}`);

  // =====================================
  // Fetch story JS
  // =====================================

  const jsRes = await fetch(jsUrl);

  if (!jsRes.ok) {

    throw new Error(
      `Failed JS fetch: ${jsRes.status}`
    );
  }

  const js = await jsRes.text();

  // =====================================
  // Extract media
  // =====================================

  const media = [];

  let isFlash = false;

  const srcMatches = [
    ...js.matchAll(
      /src:"([^"]+)"/g
    )
  ];

  for (const match of srcMatches) {

    let src = match[1];

    // Flash detection
    if (src.endsWith(".swf")) {
      isFlash = true;
    }

    // Keep media formats
    if (
      src.match(
        /\.(gif|png|jpg|jpeg|swf)$/i
      )
    ) {

      if (!src.startsWith("http")) {
        src =
          "https://homestuck.com/" + src;
      }

      if (!media.includes(src)) {
        media.push(src);
      }
    }
  }

  // =====================================
  // Extract paragraphs
  // =====================================

  const text = [];

  const pMatches = [
    ...js.matchAll(
      /t\("p",null,"([\s\S]*?)"/g
    )
  ];

  for (const match of pMatches) {

    let paragraph = match[1];

    paragraph = paragraph
      .replace(/\\n/g, "\n")
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, "\\")
      .trim();

    if (
      paragraph.length > 0 &&
      !text.includes(paragraph)
    ) {
      text.push(paragraph);
    }
  }

  // =====================================
  // Extract alt text
  // =====================================

  let alt = null;

  const altMatch = js.match(
    /alt:"([^"]+)"/
  );

  if (altMatch) {
    alt = altMatch[1];
  }

  // =====================================
  // Extract next page
  // =====================================

  let next = null;

  const nextMatch = js.match(
    /next-page-link":"\/(\d+)"/
  );

  if (nextMatch) {
    next = Number(nextMatch[1]);
  }

  console.log(`Next page: ${next}`);

  // =====================================
  // Extract next command
  // =====================================

  let nextCommand = null;

  const commandMatch = js.match(
    /"link-text":"([^"]+)"/
  );

  if (commandMatch) {
    nextCommand = commandMatch[1];
  }

  // =====================================
  // Flash placeholder
  // =====================================

  if (isFlash) {

    console.log("FLASH DETECTED");

    media.length = 0;

    media.push(
      "https://homestuck.com/panels/act-1/00001.gif"
    );

    text.push(
      "[FLASH PAGE NOT YET SUPPORTED]"
    );
  }

  // =====================================
  // Build JSON
  // =====================================

  const result = {
    page: Number(pageNum),
    type: isFlash
      ? "flash"
      : "page",
    media,
    text,
    alt,
    next,
    nextCommand
  };

  // =====================================
  // Save JSON
  // =====================================

  fs.mkdirSync("pages", {
    recursive: true
  });

  const output =
    `pages/${padded}.json`;

  fs.writeFileSync(
    output,
    JSON.stringify(result, null, 2)
  );

  console.log(`Saved ${output}`);

  // =====================================
  // Recurse
  // =====================================

  if (next) {

    // Delay to avoid hammering server
    await new Promise(r =>
      setTimeout(r, 100)
    );

    await scrapePage(next);
  }
}

// =====================================
// START
// =====================================

scrapePage(1901).catch(err => {

  console.error(err);

  process.exit(1);
});
