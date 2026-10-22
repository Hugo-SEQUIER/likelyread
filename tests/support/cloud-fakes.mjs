// Where tests/support/cloud-server.mjs serves its fake Google and test app, shared
// with the tests that drive them.
export const FAKE = "http://127.0.0.1:8796";

// An app that connects the way Claude Code does: a client metadata document, and a
// loopback redirect that matches on any port.
export const TEST_APP = {
  clientId: `${FAKE}/app/client.json`,
  name: "Likelyread test app",
  redirectUri: `${FAKE}/app/callback`,
};
