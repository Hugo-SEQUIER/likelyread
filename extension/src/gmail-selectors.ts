/**
 * Every Gmail DOM selector lives here. Gmail changes its markup without notice,
 * so when injection stops working this is the file to update.
 * Everything that uses these selectors fails safe: if nothing matches, the email
 * is sent untracked.
 */
export const SEL = {
  /** The editable body of a compose or reply window. */
  composeBody:
    'div[contenteditable="true"][role="textbox"][g_editable="true"], div.Am.Al.editable[contenteditable="true"]',
  /** The main Send button (not the schedule-send dropdown). `aoO` is Gmail's send class. */
  sendButton: 'div[role="button"].T-I.aoO',
  /** Subject input. Inline replies keep a hidden one with the thread subject. */
  subject: 'input[name="subjectbox"]',
  /** Thread title, used as a fallback subject for replies. */
  threadSubject: "h2.hP",
  /** Legacy hidden recipient inputs. */
  recipientInputs: 'input[name="to"], input[name="cc"], input[name="bcc"]',
  /** Recipient chips in the current compose UI. */
  recipientChips: '[data-hovercard-id*="@"], span[email], div[email]',
  /** The "From" row, excluded from recipients. */
  fromRow: '.az2, [name="from"], .J-J5-Ji.J-JN-M-I-Jm',
} as const;

/** How many ancestors to climb from the body to find the compose container. */
export const MAX_COMPOSE_DEPTH = 20;
