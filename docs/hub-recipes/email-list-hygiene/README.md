# email-list-hygiene

A free first pass over a lead list, before you pay a verifier or a sending tool. Each address gets
one verdict:

| verdict | means |
|---|---|
| `ok` | a work address worth verifying |
| `personal` | gmail.com, outlook.com, icloud.com and about 100 other consumer providers |
| `disposable` | a throwaway inbox (the CC0 disposable-email-domains blocklist, about 9,000 domains) |
| `role` | info@, sales@, support@ and other shared inboxes |
| `duplicate` | already earlier in the list |
| `invalid` | not an email address |

`to_verify` is the list to send on (add personal inboxes with `allow_personal: true`); `summary`
counts each verdict. Up to 2,000 addresses per call. No tool is called and no provider fee
applies: the domain lists ship with the tool as `data.csv`. $0.002 per call.

To refresh the disposable list, rebuild `data.csv` from
github.com/disposable-email-domains/disposable-email-domains and publish again.
