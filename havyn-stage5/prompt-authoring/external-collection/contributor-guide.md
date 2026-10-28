# Contributor guide — write one network request the way you'd really say it

Thanks for helping build the Havyn gold set. We need **real** requests, not
polished ones. You do not need to be a senior engineer — but you do need to be
a real person writing what you would actually ask for.

## What we ask for (10–15 minutes)

1. Think of a small network task you have really wanted (or been asked) to do:
   an office LAN, two floors, a shop with a guest Wi-Fi, a clinic, a warehouse,
   two offices that need to talk to each other, a network that broke after a
   VLAN change. Anything you have actually seen.
2. Write the request in your own words, the way you would say it to a colleague
   or post it in a forum. Plain language is better than polished language.
   If you naturally write in Roman-Urdu or mix English and Urdu, write it that
   way — that is exactly what we want.
3. Fill in the template (`submission-template.json`) with:
   - your request text in `prompt`;
   - the language you wrote it in;
   - an approximate difficulty (L1 easy → L5 hard) if you have a view;
   - `notes` for anything you would have asked back as a clarification.
4. Optional but very useful: write **2–4 variants of the same request** with
   different wording/tone — rushed, formal, confused, very short, very long.
   Same network, different human.
5. If you want to go further: write the intent (a short structured summary of
   the network: sites, areas, how many devices, VLANs, DHCP/static, routing,
   internet, wireless) — otherwise the collector drafts it with you and you
   confirm it says what you meant.

## Rules

- **No employer secrets.** Remove real hostnames, public IP addresses, customer
  and staff names, passwords, and anything under an NDA before submitting.
- **One request per file.** File it as `sub-<your-id>-<number>.json`.
- **Say what you mean, and only what you mean.** If you are unsure about a
  detail, leave it out or write your question in `notes` — do not guess.
- **Ambiguity is welcome.** A request that is genuinely under-specified,
  contradictory, or impossible is valuable: we mark it as a refusal case.
- Do not let anyone (person or AI) rewrite your prompt into "better network
  English". If a fix is needed, you fix it in your own words.

## What happens next

- Your submission goes through the real engine (schema → lint → build →
  addressing → compile). If it fails, you get the engine's exact reason and a
  chance to adjust *your* wording or the structured intent.
- A second reviewer checks that the structured intent matches your prompt
  exactly — no facts added, none dropped.
- Only then can it be adopted as `gold-101..300`, with `author` = you
  (pseudonymous id) and your consent recorded. You may withdraw before
  adoption.
