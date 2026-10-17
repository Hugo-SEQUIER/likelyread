import { test } from "node:test";
import assert from "node:assert/strict";
import { backoffMs, cleanRecipients, dnrRules, extractPixelId, normalizeServerUrl, pixelUrl } from "../src/lib";

const ID = "0f8fad5b-d9cb-469f-a165-70867728950e";

test("normalizeServerUrl", () => {
  assert.equal(normalizeServerUrl("t.example.com"), "https://t.example.com");
  assert.equal(normalizeServerUrl(" https://t.example.com/some/path/ "), "https://t.example.com");
  assert.equal(normalizeServerUrl("http://t.example.com"), null, "plain http refused for remote hosts");
  assert.equal(normalizeServerUrl("http://localhost:8000"), "http://localhost:8000");
  assert.equal(normalizeServerUrl(""), null);
  assert.equal(normalizeServerUrl("javascript:alert(1)"), null);
});

test("extractPixelId handles direct and Gmail proxy URLs", () => {
  const server = "https://t.example.com";
  assert.equal(extractPixelId(pixelUrl(server, ID), server), ID);
  const proxied = `https://ci3.googleusercontent.com/meips/ADKq_NbXyz=s0-d-e1-ft#https://t.example.com/p/${ID}.gif`;
  assert.equal(extractPixelId(proxied, server), ID);
  assert.equal(extractPixelId(`https://evil-t.example.com/p/${ID}.gif`, server), null);
  assert.equal(extractPixelId(`https://t.example.com.evil.io/p/${ID}.gif`, server), null);
  assert.equal(extractPixelId("https://t.example.com/p/not-a-uuid.gif", server), null);
  assert.equal(extractPixelId(`https://txexample.com/p/${ID}.gif`, server), null, "dots are escaped");
});

test("backoff grows and caps at 6 hours", () => {
  assert.equal(backoffMs(1), 30_000);
  assert.equal(backoffMs(2), 60_000);
  assert.equal(backoffMs(50), 6 * 3600 * 1000);
});

test("cleanRecipients dedupes and parses names", () => {
  assert.deepEqual(cleanRecipients(["Ann <Ann@X.com>, bob@y.io", "bob@y.io", "", "not an email"]), ["ann@x.com", "bob@y.io"]);
});

test("dnrRules block the direct pixel and the proxied form", () => {
  const rules = dnrRules("https://t.example.com");
  assert.equal(rules[0].condition.urlFilter, "|https://t.example.com/p/");
  const re = new RegExp(rules[1].condition.regexFilter!);
  assert.ok(re.test(`https://ci3.googleusercontent.com/meips/abc#https://t.example.com/p/${ID}.gif`));
  assert.ok(!re.test(`https://ci3.googleusercontent.com/meips/abc#https://other.com/p/${ID}.gif`));
});
