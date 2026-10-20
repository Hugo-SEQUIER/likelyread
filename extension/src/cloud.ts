/** The hosted Likelyread service. Builds can point elsewhere with CLOUD_URL=... */
declare const __CLOUD_URL__: string | undefined;

export const CLOUD_URL: string = typeof __CLOUD_URL__ === "string" && __CLOUD_URL__ ? __CLOUD_URL__ : "https://app.likelyread.com";

/** Where the options page sends the user to sign in with Google. */
export function cloudSignInUrl(extensionId: string): string {
  return `${CLOUD_URL}/auth/google?ext=${encodeURIComponent(extensionId)}`;
}
