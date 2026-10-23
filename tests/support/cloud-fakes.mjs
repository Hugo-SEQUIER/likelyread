// Where tests/support/cloud-server.mjs serves its fake Google and test app, shared
// with the tests that drive them.
export const FAKE = "http://127.0.0.1:8796";

// Who fake Google signs in: Ann, whose address is in ADMIN_EMAILS, unless the browser
// carries the cookie e2e_person=bob for 127.0.0.1. Bob is not an admin.
export const PEOPLE = { ann: "ann@example.com", bob: "bob@example.com" };

// The reviewer sign-in (server/review.py) is on, so an app's sign-in shows Google and the
// reviewer's form. Test values, not a secret.
export const REVIEWER = { email: "reviewer@likelyread.example", password: "e2e-review-password-0123" };

// An app that connects the way Claude Code does: a client metadata document, and a
// loopback redirect that matches on any port.
export const TEST_APP = {
  clientId: `${FAKE}/app/client.json`,
  name: "Likelyread test app",
  redirectUri: `${FAKE}/app/callback`,
};
