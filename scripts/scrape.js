const fs = require("fs");
const { chromium } = require("playwright");

console.log("STARTING PLAYWRIGHT SCRAPER");

const visited = new Set();

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

  return new Promise(async (resolve) => {

    let resolved = false;

    // Listen for ALL requests
    page.on("request", request => {

      const url = request.url();

      // We want:
      // 001901HS-xxxxx.js

      if (
        url.includes(`${padded}HS-`) &&
        url.endsWith(".js")
      ) {

        if (!resolved) {

          resolved = true;

          console.log(
            `Discovered module: ${url}`
          );

          resolve(url);
        }
      }
    });

    // Open page
    await page.goto(
      `https://homestuck.com/${padded}`,
      {
        waitUntil: "networkidle",
        timeout: 60000
      }
    );

    // Safety timeout
    setTimeout(() => {

      if (!resolved) {

        console.log(
          `Module timeout for ${padded}`
        );

        resolve(null);
      }

    }, 10000);
  });
}

// =====================================
// Scrape page
// =====================================

async function scrapePage(browser, pageNum) {

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
  // Fetch JS directly
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

    if (
      src.toLowerCase()
        .endsWith(".swf")
    ) {
      isFlash = true;
    }

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
  // Extract text
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
  // Extract alt
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
  // Flash placeholder
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
  // Save JSON
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

  await page.close();

  // =====================================
  // Recurse
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
      1901
    );

  } finally {

    await browser.close();
  }

})().catch(err => {

  console.error(err);

  process.exit(1);

});
