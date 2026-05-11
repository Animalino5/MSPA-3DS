const fs = require("fs");

async function run() {

  const page = "001901";

  // =========================
  // Fetch story JS directly
  // =========================

  const jsUrl =
    "https://homestuck.com/assets/001901HS-bLwrJzIw.js";

  console.log("Fetching:", jsUrl);

  const response = await fetch(jsUrl);

  if (!response.ok) {
    throw new Error(
      `HTTP ${response.status}`
    );
  }

  const js = await response.text();

  // =========================
  // Extract images
  // =========================

  const media = [];

  const imgMatches = [
    ...js.matchAll(
      /src:"([^"]+\.(?:gif|png|jpg|jpeg))"/g
    )
  ];

  for (const match of imgMatches) {

    let src = match[1];

    if (!src.startsWith("http")) {
      src =
        "https://homestuck.com/" + src;
    }

    media.push(src);
  }

  // =========================
  // Extract paragraphs
  // =========================

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

    text.push(paragraph);
  }

  // =========================
  // Extract next page
  // =========================

  let next = null;

  const nextMatch = js.match(
    /next-page-link":"\/(\d+)"/
  );

  if (nextMatch) {
    next = Number(nextMatch[1]);
  }

  // =========================
  // Extract alt text
  // =========================

  let alt = null;

  const altMatch = js.match(
    /alt:"([^"]+)"/
  );

  if (altMatch) {
    alt = altMatch[1];
  }

  // =========================
  // Build JSON
  // =========================

  const result = {
    page: Number(page),
    media,
    text,
    alt,
    next
  };

  // =========================
  // Save file
  // =========================

  fs.writeFileSync(
    `pages/${page}.json`,
    JSON.stringify(result, null, 2)
  );

  console.log("Saved page JSON.");
}

run().catch(err => {
  console.error(err);
  process.exit(1);
});
