# Biomem privacy policy

Last updated: 29 August 2026

This policy describes how the Biomem browser extension handles user data.
Biomem is a local-first memory tool for supported LLM websites. It does not
require a Biomem account, registration, API key, or hosted authentication
service.

## Data the extension handles

To provide its memory features, the extension handles:

- text that the user submits to a supported LLM website;
- assistant responses and memory-summary markers present in those responses;
- locally generated conversation summaries and recalled memory text;
- the hostname of the supported LLM website as local provenance; and
- extension settings, including enabled sites and the local Biomem service
  address.

Conversation text may contain personal information chosen by the user. The
extension does not read or store passwords, authentication cookies, payment
details, precise location, or unrelated browsing activity.

## How the data is used

The extension uses the submitted text to retrieve relevant memories from the
local Biomem service. It may add recalled memory text to the prompt that the
user chooses to submit. It uses assistant-produced summaries to update the
local memory database. These uses are limited to Biomem's user-facing purpose
of providing persistent conversational context.

## Storage and sharing

The extension communicates with the Biomem service through a loopback address
on the user's own computer. Memories and embeddings are stored and processed
locally. Biomem does not operate a remote memory service, receive the user's
conversation data, sell it, use it for advertising, or use it for credit or
lending decisions.

When the user submits an enriched prompt, that prompt—including any recalled
memory included in it—is transmitted to the LLM website selected by the user.
That transmission is necessary to provide the extension's single purpose and
is governed by the selected website's own privacy terms. The Biomem publisher
does not receive that data.

The extension does not otherwise transfer user data to third parties. It does
not load or execute remote code.

## Retention and deletion

Conversation memories remain in the user's local Biomem database until the
user deletes them or removes the local database. Extension settings remain in
the browser profile until they are reset, cleared, or the extension is
removed.

## Security

The local HTTP service accepts only loopback connections and restricts browser
requests to approved extension origins. Public web pages cannot call the local
memory API directly. Responses are marked as non-cacheable, and the extension
does not relay data to a publisher-operated server.

## Chrome Web Store Limited Use

The use of information received from Chrome APIs will adhere to the Chrome Web
Store User Data Policy, including the Limited Use requirements. User data is
used only to provide or improve Biomem's single user-facing purpose.

## Contact

Questions about this policy can be sent to
[petr.glaser@bleeding.dev](mailto:petr.glaser@bleeding.dev) or filed through
the [Biomem issue tracker](https://github.com/BleedingDev/biomem/issues).
