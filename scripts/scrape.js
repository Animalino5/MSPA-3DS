const fs = require("fs");

const visited = new Set();

async function scrapePage(pageNum) {

  // Prevent duplicates
  if (visited.has(pageNum)) {
    return;
  }

  visited.add(pageNum);

  const padded =
    String(pageNum).padStart(6, "0");

  console.log(`Scraping ${padded}...`);

  // =====================================
  // STEP 1:
  // Fetch page HTML
  // =====================================

  const pageRes = await fetch(
    `https://homestuck.com/${padded}`
  );

  if (!pageRes.ok) {
    throw new Error(
      `Failed page fetch: ${pageRes.status}`
    );
  }

  const html = await pageRes.text();

  // =====================================
  // STEP 2:
  // Find page JS module
  // =====================================

  const jsMatch = html.match(
    new RegExp(
      `${padded}HS-[A-Za-z0-9_-]+\\.js`
    )
  );

  if (!jsMatch) {

    console.log(
      `No JS module found for ${padded}`
    );

    return;
  }

  const jsFile = jsMatch[0];

  const jsUrl =
    `https://homestuck.com/assets/${jsFile}`;

  console.log(`Using ${jsFile}`);

  // =====================================
  // STEP 3:
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
  // STEP 4:
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

    // Detect SWFs
    if (src.endsWith(".swf")) {
      isFlash = true;
    }

    // Only keep image formats
    if (
      src.match(/\.(gif|png|jpg|jpeg|swf)$/i)
    ) {

      if (!src.startsWith("http")) {
        src =
          "https://homestuck.com/" + src;
      }

      media.push(src);
    }
  }

  // =====================================
  // STEP 5:
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
  // STEP 6:
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
  // STEP 7:
  // Extract next page
  // =====================================

  let next = null;

  const nextMatch = js.match(
    /next-page-link":"\/(\d+)"/
  );

  if (nextMatch) {
    next = Number(nextMatch[1]);
  }

  // =====================================
  // STEP 8:
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
  // STEP 9:
  // Flash placeholder logic
  // =====================================

  if (isFlash) {

    console.log(
      `Flash detected on ${padded}`
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
  // STEP 10:
  // Build JSON
  // =====================================

  const result = {
    page: Number(pageNum),
    type: isFlash ? "flash" : "page",
    media,
    text,
    alt,
    next,
    nextCommand
  };

  // =====================================
  // STEP 11:
  // Save JSON
  // =====================================

  fs.mkdirSync("pages", {
    recursive: true
  });

  fs.writeFileSync(
    `pages/${padded}.json`,
    JSON.stringify(result, null, 2)
  );

  console.log(
    `Saved pages/${padded}.json`
  );

  // =====================================
  // STEP 12:
  // Recursive scrape
  // =====================================

  if (next) {

    // Small delay to avoid hammering site
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
