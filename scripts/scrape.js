const fs = require("fs");

console.log("STARTING SCRAPER");

const visited = new Set();

async function scrapePage(pageNum) {

  // Prevent duplicate pages
  if (visited.has(pageNum)) {
    return;
  }

  visited.add(pageNum);

  const padded =
    String(pageNum).padStart(6, "0");

  console.log(`\n=== PAGE ${padded} ===`);

  // =====================================
  // TEMP MODULE DATABASE
  // =====================================
  //
  // For now we only know 001901.
  // Later we'll automate this.
  //

  const knownModules = {
    "001901":
      "001901HS-bLwrJzIw.js"
  };

  const jsFile =
    knownModules[padded];

  if (!jsFile) {

    console.log(
      `No module known for ${padded}`
    );

    return;
  }

  const jsUrl =
    `https://homestuck.com/assets/${jsFile}`;

  console.log(`Fetching JS: ${jsUrl}`);

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

    // Detect flashes
    if (src.endsWith(".swf")) {
      isFlash = true;
    }

    // Keep supported media
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
      .replace(/\\"/g, "\"")
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
  // Extract next command text
  // =====================================

  let nextCommand = null;

  const commandMatch = js.match(
    /"link-text":"([^"]+)"/
  );

  if (commandMatch) {
    nextCommand = commandMatch[1];
  }

  // =====================================
  // Flash placeholder logic
  // =====================================

  if (isFlash) {

    console.log(
      `FLASH DETECTED`
    );

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

  console.log(
    `Saved ${output}`
  );

  // =====================================
  // Recursive scrape
  // =====================================

  if (next) {

    console.log(
      `Waiting before next page...`
    );

    await new Promise(r =>
      setTimeout(r, 250)
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
