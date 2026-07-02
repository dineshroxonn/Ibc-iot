/*
 * SensorChain demo video recorder.
 *
 * Records a single ~2.5 minute browser session (1280×720 webm) walking
 * through the live system: title/architecture slides, replays of the
 * real e2e and MQTT/oracle terminal output, the citizen portal running
 * against the live Fabric network (verify → tamper → anomaly view),
 * and the Grafana operations dashboard.
 *
 * Prereqs: Fabric network + bridge + fabric_api (:8100) + Grafana (:3000) up.
 *   node demo/record_demo.js   → demo/out/sensorchain-demo.webm
 */

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright-core");

const OUT_DIR = path.join(__dirname, "out");
const SCENES = "file://" + path.join(__dirname, "scenes.html");
const PORTAL = process.env.PORTAL_URL || "http://127.0.0.1:8100/";
const GRAFANA = process.env.GRAFANA_URL || "http://127.0.0.1:3000/d/sensorchain/?kiosk";
const DEMO_BATCH = process.env.DEMO_BATCH || "gw-edge-live02-batch-000002";

const E2E_LINES = [
  '<span class="acc">[1]</span> register gateway device gw-pune-87bac373 <span class="dim">(DeviceRegistry contract)</span>',
  '    on-chain: status=active fingerprint=1448bb0001ea0f4d',
  '<span class="acc">[2]</span> anchor calibration certificate <span class="dim">(Calibration contract)</span>',
  '    calibrated=true cert=b53f2c6dc37f49d1…',
  '<span class="acc">[3]</span> anchor a 60-reading batch, signed by the gateway HSM',
  '    anchored root=e3ea5deb414e5643b66a36bd… count=60',
  '<span class="acc">[4]</span> verify honest + tampered readings against the on-chain root',
  '    honest reading #7   : <span class="ok">verified=true</span>',
  '    tampered (42.000)   : <span class="bad">verified=false — data altered after anchoring</span>',
  '<span class="acc">[5]</span> submit an anchor signed by the WRONG key',
  '    <span class="ok">rejected as expected: invalid signature</span>',
  '<span class="acc">[6]</span> register SLA, record breaching metrics, catch SLABreach event',
  '    breached=true violations=2 <span class="warn">penalty=₹1,00,000</span>',
  '    SLABreach event from block 22: min_completeness_pct, max_silence_minutes',
  '<span class="acc">[7]</span> firmware: manufacturer-signed release → approval → attestation',
  '    before approval     : <span class="warn">device refuses image (not_approved_for_rollout)</span>',
  '    after approval      : image verified=true · rollout attested ok=true',
  '    trojaned install    : <span class="bad">flagged on-chain (installed_hash_mismatch)</span>',
  '<span class="acc">[8]</span> lifecycle: rotate the gateway key (signed by the old key)',
  '    old key anchor: <span class="bad">rejected</span> · new key anchor: <span class="ok">accepted</span>',
  '<span class="acc">[9]</span> vendor transfer → decommission',
  '    post-decommission anchor: <span class="ok">rejected as expected (retired)</span>',
  '&nbsp;',
  '<span class="ok">END-TO-END LIFECYCLE ON FABRIC: PASSED</span>',
];

const MQTT_LINES = [
  '<span class="dim">[fleet]</span> registered 12 sensors on-chain (11 calibrated, 1 left uncalibrated)',
  '<span class="dim">[fleet]</span> <span class="warn">faults armed: aqi-0001 under-reports from t=82s; aqi-0002 silent from t=75s</span>',
  '<span class="dim">[edge]</span>  registered gw-edge-live02 on-chain (fingerprint 0fceee349265ed46)',
  '<span class="dim">[edge]</span>  anchored batch-000001: 120 readings root=02127af41fac8db7…',
  '<span class="dim">[oracle]</span> baselined aqi-0001: threshold 1.609 (25 readings)',
  '<span class="dim">[edge]</span>  anchored batch-000002: 120 readings root=424ee8b181d28211…',
  '<span class="dim">[edge]</span>  anchored batch-000003: 115 readings root=3f6b2058a02e0c3d…',
  '<span class="dim">[oracle]</span> <span class="bad">ON-CHAIN [CRITICAL] temporal aqi-0001: error 8.792 = 5.5× threshold — possible tampering</span>',
  '<span class="dim">[oracle]</span> <span class="warn">ON-CHAIN [WARNING] spatial aqi-0001: diverges from co-located consensus (z=6.1)</span>',
  '<span class="dim">[edge]</span>  anchored batch-000004: 110 readings root=ffc3b285809e457d…',
  '<span class="dim">[oracle]</span> <span class="bad">ON-CHAIN [CRITICAL] silence aqi-0002: no readings for 1.1 min</span>',
  '<span class="dim">[edge]</span>  anchored batch-000005: 110 readings root=10de05c0c1dabd52…',
  '&nbsp;',
  '<span class="dim">[edge]</span>  575 readings · 5 batches · 0 errors · <span class="ok">0.15% CPU · 29.1 MiB peak RSS</span>',
  '<span class="dim">[oracle]</span> <span class="ok">11 anomalies anchored on-chain (reported_by=CitySPVMSP)</span>',
];

const hold = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium" });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    recordVideo: { dir: OUT_DIR, size: { width: 1280, height: 720 } },
  });
  const page = await context.newPage();

  // -- slides -----------------------------------------------------------
  await page.goto(SCENES);
  await page.evaluate(() => window.demo.show("title"));
  await hold(5500);
  await page.evaluate(() => window.demo.show("arch"));
  await hold(11000);

  // -- terminal replay: e2e ---------------------------------------------
  await page.evaluate(() => window.demo.show("term1"));
  await hold(1200);
  await page.evaluate(([lines]) => window.demo.type("term1-body", lines, 800), [E2E_LINES]);
  await hold(E2E_LINES.length * 800 + 3000);

  // -- terminal replay: MQTT + oracle -------------------------------------
  await page.evaluate(() => window.demo.show("term2"));
  await hold(1200);
  await page.evaluate(([lines]) => window.demo.type("term2-body", lines, 900), [MQTT_LINES]);
  await hold(MQTT_LINES.length * 900 + 3000);

  // -- live citizen portal ---------------------------------------------------
  await page.goto(PORTAL);
  await hold(2500);
  await page.click("#listBtn");
  await hold(2000);
  await page.fill("#batchId", DEMO_BATCH);
  await page.fill("#readingIndex", "3");
  await page.click("#fetchBtn");
  await page.waitForFunction(() => document.getElementById("root").value.length === 64);
  await hold(1500);
  await page.click("#verifyBtn");
  await page.waitForSelector("#verifyOut.ok");
  await hold(3500);

  // tamper with the reading, watch it fail
  const reading = JSON.parse(await page.inputValue("#reading"));
  reading.value = "42.000";
  await page.fill("#reading", JSON.stringify(reading, null, 2));
  await hold(1000);
  await page.click("#verifyBtn");
  await page.waitForSelector("#verifyOut.bad");
  await hold(3500);

  // on-chain anomaly findings in the portal
  await page.click("#anomBtn");
  await page.waitForSelector("#statusOut table");
  await hold(3000);
  await page.evaluate(() => document.querySelector("#statusOut").scrollIntoView({ block: "start" }));
  await page.mouse.wheel(0, 500);
  await hold(3000);

  // -- Grafana operations dashboard ----------------------------------------
  await page.goto(GRAFANA, { waitUntil: "networkidle" }).catch(() => {});
  await hold(6000);
  await page.mouse.wheel(0, 400);
  await hold(4000);

  // -- results + close ---------------------------------------------------------
  await page.goto(SCENES);
  await page.evaluate(() => window.demo.show("results"));
  await hold(10000);
  await page.evaluate(() => window.demo.show("close"));
  await hold(5000);

  await context.close();   // flushes the video
  await browser.close();

  // playwright names the file with a hash — rename to something stable
  const video = fs.readdirSync(OUT_DIR).find((f) => f.endsWith(".webm"));
  const target = path.join(OUT_DIR, "sensorchain-demo.webm");
  if (video && video !== "sensorchain-demo.webm") {
    fs.renameSync(path.join(OUT_DIR, video), target);
  }
  console.log("video written:", target,
    `(${(fs.statSync(target).size / 1e6).toFixed(1)} MB)`);
}

main().catch((err) => { console.error("RECORDING FAILED:", err); process.exit(1); });
