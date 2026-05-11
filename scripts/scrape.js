const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const START_PAGE = 1901;
const END_PAGE = 2659;

const SAVE_EVERY = 10;

const OUT_DIR = path.join(__dirname, "..", "pages");

if (!fs.existsSync(OUT_DIR)) {
    fs.mkdirSync(OUT_DIR, { recursive: true });
}

function padPage(num) {
    return String(num).padStart(6, "0");
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function extractText(js) {
    const text = [];

    // catches:
    // t("p",null,"TEXT",-1)
    // c("p",null,"TEXT",-1)
    const regex = /["']p["'],null,"((?:\\.|[^"\\])*)"/g;

    let match;

    while ((match = regex.exec(js)) !== null) {
        let str = match[1];

        str = str
            .replace(/\\"/g, '"')
            .replace(/\\n/g, "\n")
            .replace(/\\\\/g, "\\");

        str = str.trim();

        if (str.length > 0) {
            text.push(str);
        }
    }

    return text;
}

function extractMedia(js) {
    const media = [];

    // src:"panels/act-1/00001.gif"
    const regex = /src:"([^"]+)"/g;

    let match;

    while ((match = regex.exec(js)) !== null) {
        let url = match[1];

        if (!url.startsWith("http")) {
            url = "https://homestuck.com/" + url;
        }

        media.push(url);
    }

    return media;
}

function extractAlt(js) {
    const match = js.match(/alt:"((?:\\.|[^"\\])*)"/);

    if (!match) return "";

    return match[1]
        .replace(/\\"/g, '"')
        .replace(/\\n/g, "\n")
        .replace(/\\\\/g, "\\")
        .trim();
}

function extractNext(js) {
    const match = js.match(/next-page-link":"\/(\d+)"/);

    if (!match) return null;

    return parseInt(match[1]);
}

function extractCommand(js) {
    const match = js.match(/link-text":"((?:\\.|[^"\\])*)"/);

    if (!match) return "";

    return match[1]
        .replace(/\\"/g, '"')
        .replace(/\\n/g, "\n")
        .replace(/\\\\/g, "\\")
        .trim();
}

function isFlash(media) {
    return media.some(m =>
        m.endsWith(".swf") ||
        m.includes("scratch/")
    );
}

async function discoverModule(page, pageNum) {
    const padded = padPage(pageNum);

    const responses = [];

    page.on("response", async response => {
        try {
            const url = response.url();

            if (
                url.includes("/assets/") &&
                url.includes(`${padded}HS-`) &&
                url.endsWith(".js")
            ) {
                responses.push(url);
            }
        } catch {}
    });

    await page.goto(`https://homestuck.com/${padded}`, {
        waitUntil: "networkidle",
        timeout: 60000
    });

    await sleep(3000);

    if (responses.length === 0) {
        return null;
    }

    return responses[0];
}

async function scrapePage(browser, pageNum) {
    const padded = padPage(pageNum);

    console.log(`\n=== PAGE ${padded} ===`);

    const page = await browser.newPage();

    try {
        const moduleUrl = await discoverModule(page, pageNum);

        if (!moduleUrl) {
            console.log(`Could not locate module for ${padded}`);

            return null;
        }

        console.log(`Discovered module: ${moduleUrl}`);

        const js = await page.evaluate(async (url) => {
            const res = await fetch(url);
            return await res.text();
        }, moduleUrl);

        const text = extractText(js);
        const media = extractMedia(js);
        const alt = extractAlt(js);
        const next = extractNext(js);
        const command = extractCommand(js);

        let finalMedia = media;

        if (isFlash(media)) {
            console.log("FLASH DETECTED");

            finalMedia = [
                "https://homestuck.com/panels/act-1/00001.gif"
            ];
        }

        const data = {
            page: pageNum,
            media: finalMedia,
            text,
            alt,
            command,
            next
        };

        const outPath = path.join(
            OUT_DIR,
            `${padded}.json`
        );

        fs.writeFileSync(
            outPath,
            JSON.stringify(data, null, 2)
        );

        console.log(`Saved ${padded}.json`);
        console.log(`Text paragraphs: ${text.length}`);

        return next;
    } catch (err) {
        console.error(err);
        return null;
    } finally {
        await page.close();
    }
}

(async () => {
    console.log("STARTING PLAYWRIGHT SCRAPER");

    const browser = await chromium.launch({
        headless: true
    });

    try {
        let count = 0;

        for (
            let current = START_PAGE;
            current <= END_PAGE;
            current++
        ) {
            const padded = padPage(current);

            const existing = path.join(
                OUT_DIR,
                `${padded}.json`
            );

            if (fs.existsSync(existing)) {
                console.log(`Skipping existing ${padded}`);
                continue;
            }

            await scrapePage(browser, current);

            count++;

            if (count % SAVE_EVERY === 0) {
                console.log(
                    `\n=== CHECKPOINT: ${count} pages scraped ===`
                );
            }

            // don't hammer the site
            await sleep(1500);
        }
    } finally {
        await browser.close();
    }

    console.log("SCRAPE COMPLETE");
})();
