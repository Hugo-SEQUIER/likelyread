/**
 * Every Gmail DOM selector lives here. Gmail changes its markup without notice,
 * so when injection stops working this is the file to update.
 * Everything that uses these selectors fails safe: if nothing matches, the email
 * is sent untracked.
 *
 * Cross-checked against gmail.js (github.com/KartikTalwar/gmail.js), which is
 * maintained against live Gmail.
 */
export const SEL = {
  /** The whole compose or reply window (recipients, subject, body and toolbar). */
  composeRoot: "div.M9",
  /** The editable body of a compose or reply window. */
  composeBody:
    'div[contenteditable="true"][role="textbox"][g_editable="true"], div.Am.Al.editable[contenteditable="true"]',
  /** The main Send button (not the schedule-send dropdown). `aoO` is Gmail's send class. */
  sendButton: 'div[role="button"].T-I.aoO',
  /** Subject input. Inline replies use name="subject". */
  subject: 'input[name="subjectbox"], input[name="subject"]',
  /** Thread title, used as a fallback subject for replies. */
  threadSubject: "h2.hP",
  /** Recipient chips in the current ("peoplekit") compose UI. */
  recipientChips:
    'tr.bzf div[name] [data-hovercard-id], div[name="to"] [data-hovercard-id], div[name="cc"] [data-hovercard-id], div[name="bcc"] [data-hovercard-id]',
  /** Recipients in the older compose UI. */
  recipientInputs: '.GS input[type="hidden"], input[type="hidden"][name="to"], input[type="hidden"][name="cc"], input[type="hidden"][name="bcc"]',
  /** Last-resort recipient markers. */
  recipientFallback: "span[email], div[email]",
  /** The "From" field when the account has several send-as addresses. */
  fromInput: 'input[name="from"]',
  /** Areas that never hold recipients. */
  notRecipients: '.az2, [name="from"], .gmail_quote, blockquote',
  /** One message in an opened conversation, and the sender line inside it. */
  messageContainer: "div.adn, div.gs",
  messageSender: "span.gD[email]",
} as const;

/** How many ancestors to climb from the body when there is no div.M9. */
export const MAX_COMPOSE_DEPTH = 20;
