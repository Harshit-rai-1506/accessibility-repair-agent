# Real public pages: before and after

Made by `python scripts/public_pages.py`: the agent with tools and guardrails (the `agent_guarded` method) on a snapshot of each home page. Scores are axe-core's violation score (impact-weighted, as in the benchmark) on the snapshot, scanned offline before and after the fix; the live score, with the site's stylesheets and images, is shown for reference. One page per site, so these are examples, not a sample.

| Site | Category | Live score | Rules failing (snapshot) | Score before → after | Decrease | Rules fixed / tried | Still failing | Rejected attempts | Review flags | Tokens | Time |
|---|---|---|---|---|---|---|---|---|---|---|---|
| [www.snapdeal.com](www.snapdeal.com.md) | e-commerce | 39 | 7 | 27 → 3 | 0.89 | 6 / 7 | 1 | 0 | 2 | 1,131,470 | 3:07 |
| [www.du.ac.in](www.du.ac.in.md) | education | 61 | 14 | 54 → 29 | 0.46 | 7 / 14 | 7 | 3 | 3 | 334,648 | 28:58 |
| [www.iitb.ac.in](www.iitb.ac.in.md) | education | 23 | 6 | 24 → 9 | 0.62 | 4 / 6 | 2 | 0 | 2 | 326,255 | 6:48 |
| [www.mygov.in](www.mygov.in.md) | government | 42 | 9 | 30 → 7 | 0.77 | 7 / 9 | 2 | 3 | 2 | 1,659,825 | 5:35 |
| [www.aiims.edu](www.aiims.edu.md) | hospital | 35 | 9 | 37 → 11 | 0.70 | 6 / 9 | 3 | 1 | 2 | 2,323,002 | 8:46 |

Over 5 page(s) with violations: violation score 172 → 59 (decrease 0.66; median per page 0.70). Rules fixed: 30 of 45 tried. Blocked at the input screen: 0.

By rule, over all pages:

| Rule | Pages | Fixed | Improved | Not fixed |
|---|---|---|---|---|
| region | 4 | 2 | 0 | 2 |
| target-size | 4 | 1 | 0 | 3 |
| heading-order | 3 | 2 | 0 | 1 |
| image-alt | 3 | 3 | 0 | 0 |
| link-name | 3 | 1 | 0 | 2 |
| aria-allowed-attr | 2 | 2 | 0 | 0 |
| aria-required-children | 2 | 0 | 0 | 2 |
| frame-title | 2 | 2 | 0 | 0 |
| label | 2 | 2 | 0 | 0 |
| landmark-one-main | 2 | 2 | 0 | 0 |
| landmark-unique | 2 | 2 | 0 | 0 |
| list | 2 | 1 | 0 | 1 |
| page-has-heading-one | 2 | 1 | 0 | 1 |
| select-name | 2 | 2 | 0 | 0 |
| aria-allowed-role | 1 | 1 | 0 | 0 |
| aria-required-parent | 1 | 0 | 0 | 1 |
| button-name | 1 | 1 | 0 | 0 |
| color-contrast | 1 | 0 | 0 | 1 |
| color-contrast-enhanced | 1 | 1 | 0 | 0 |
| empty-heading | 1 | 1 | 0 | 0 |
| image-redundant-alt | 1 | 1 | 0 | 0 |
| listitem | 1 | 0 | 0 | 1 |
| marquee | 1 | 1 | 0 | 0 |
| presentation-role-conflict | 1 | 1 | 0 | 0 |

Not tested:

- https://uidai.gov.in/ (government): the site answered HTTP 500
- https://www.apollohospitals.com/ (hospital): the site answered HTTP 403
- https://www.flipkart.com/ (e-commerce): could not load it: Page.add_script_tag: Executing inline script violates the following Content Security Policy directive 'script-src 'self' 'unsafe-inline' 'unsafe-eval' https://*
- https://www.india.gov.in/ (government): the site answered HTTP 403
- https://www.mohfw.gov.in/ (government): the site answered HTTP 403
- https://www.nykaa.com/ (e-commerce): could not load it: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at https://www.nykaa.com/
- https://www.passportindia.gov.in/ (government): could not load it: Page.add_script_tag: Executing inline script violates the following Content Security Policy directive 'script-src 'self' https://platform.twitter.com https://co
