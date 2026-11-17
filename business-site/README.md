# Top3 business site

Static pages that satisfy Meta's Business Policy review requirement for a
WhatsApp Business Account: a public website that describes the business,
explains how WhatsApp is used, and publishes a privacy policy.

## Before deploying — replace every placeholder

Search for `[` and fill in all of these:

- `[STREET ADDRESS]`, `[PIN]` — same address as in the Business Manager profile
- `[+91 XXXXXXXXXX]` — contact number

Legal name (Kamalanathan Raman, sole proprietor) and dates are already filled in.
These must match Meta Business Settings > Business info exactly.

Check nothing remains:

    grep -rn '\[' *.html

## Deploy on Vercel

    git init && git add . && git commit -m "business site"
    # push to a GitHub repo, then:
    # vercel.com > Add New > Project > import the repo
    # Framework preset: Other. Root directory: business-site (or repo root)
    # No build command, no output directory - it is plain static HTML.

Or from the CLI:

    npx vercel --prod

## After it is live

1. Confirm all three pages load over https.
2. Meta Business Settings > Business info: set the website to the Vercel URL,
   and make sure the legal name, address and phone match this page exactly.
3. WhatsApp account > Request review.
