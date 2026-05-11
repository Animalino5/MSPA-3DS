const fs = require("fs");
const { chromium } = require("playwright");

console.log("STARTING PLAYWRIGHT SCRAPER");

const visited = new Set();

// =====================================
// CONFIG
// =====================================

const START_PAGE = 1901;
const END_PAGE = 2659;

// =====================================
// Launch browser
// =====================================

async function createBrowser() {

  return await chromium.launch({
    headless: true
  });
}

// =====================================
// Discover module URL
// =====================================

async function discoverModule(page, padded) {

  let moduleUrl = null;

  const listener = request => {

    const url = request.url();

    // We want:
    // 001901HS-xxxxx.js

    if (
      url.includes(`${padded}HS-`) &&
      url.endsWith(".js")
    ) {

      moduleUrl = url;

      console.log(
        `Discovered module: ${url}`
      );
    }
  };

  // Add listener
  page.on("request", listener);

  // Open page
  await page.goto(
    `https://homestuck.com/${padded}`,
    {
      waitUntil: "networkidle",
      timeout: 60000
    }
  );

  // Allow requests to settle
  await page.waitForTimeout(2000);

  // Remove listener
  page.off("request", listener);

  return moduleUrl;
}

// =====================================
// Scrape page
// =====================================

async function scrapePage(browser, pageNum) {

  // Stop recursion
  if (pageNum > END_PAGE) {

    console.log(
      "Reached end target."
    );

    return;
  }

  // Prevent duplicate pages
  if (visited.has(pageNum)) {
    return;
  }

  visited.add(pageNum);

  const padded =
    String(pageNum).padStart(6, "0");

  console.log(`\n=== PAGE ${padded} ===`);

  const page =
    await browser.newPage();

  // =====================================
  // Discover module
  // =====================================

  const moduleUrl =
    await discoverModule(
      page,
      padded
    );

  if (!moduleUrl) {

    console.log(
      `Failed to discover module`
    );

    await page.close();

    return;
  }

  // =====================================
  // Fetch module JS
  // =====================================

  console.log(
    `Fetching module JS...`
  );

  const jsRes =
    await fetch(moduleUrl);

  if (!jsRes.ok) {

    throw new Error(
      `Module fetch failed: ${jsRes.status}`
    );
  }

  const js =
    await jsRes.text();

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
    if (
      src.toLowerCase()
        .endsWith(".swf")
    ) {
      isFlash = true;
    }

    // Keep supported formats
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

  console.log(
    `Next page: ${next}`
  );

  // =====================================
  // Extract next command
  // =====================================

  let nextCommand = null;

  const commandMatch = js.match(
    /"link-text":"([^"]+)"/
  );

  if (commandMatch) {
    nextCommand =
      commandMatch[1];
  }

  // =====================================
  // Flash placeholder logic
  // =====================================

  if (isFlash) {

    console.log(
      "FLASH DETECTED"
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
  // Cleanup page
  // =====================================

  await page.close();

  // =====================================
  // Recursive scrape
  // =====================================

  if (next) {

    await new Promise(r =>
      setTimeout(r, 100)
    );

    await scrapePage(
      browser,
      next
    );
  }
}

// =====================================
// START
// =====================================

(async () => {

  const browser =
    await createBrowser();

  try {

    await scrapePage(
      browser,
      START_PAGE
    );

  } finally {

    await browser.close();
  }

})().catch(err => {

  console.error(err);

  process.exit(1);

});
