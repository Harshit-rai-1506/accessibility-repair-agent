# Failures: agent_guarded on accessguru test (mistralai:mistral-small-2603)

Made with `python -m a11y_agent.evaluate failures --save`.

34 of 138 items not resolved by agent_guarded.

| Why | Items |
|---|---|
| edits did not match the page | 11 |
| fixed the target but broke another rule | 10 |
| the target still fails | 7 |
| every attempt rejected by the guardrails | 6 |

| Item | Rule | Page-level | Why | Last step | Guardrails |
|---|---|---|---|---|---|
| accessguru-043 | region | yes | edits did not match the page | propose: edits could not be applied: Edit 5: the find text was not found (it begins "<span>Shop By Category</span> <a href="/collections/shop-all-categories" class=…"). Copy it exactly from the HTML shown; the HTML may b | no objections |
| accessguru-050 | region | yes | the target still fails | verify: The rule "region" still fails: <a id="psMainLogo" href="/" aria-label="Pluralsight" data-aa-title="PN-Pluralsight" tabindex="0"> | no objections |
| accessguru-051 | region | yes | edits did not match the page | propose: edits could not be applied: Edit 4: the find text was not found (it begins "<div data-w-id="b4fc15a9-88b1-05f5-f944-d083aecae3df" style="opacity: 0;" class…"). Copy it exactly from the HTML shown; the HTML may b | rejected broken_markup 1 |
| accessguru-052 | region | yes | every attempt rejected by the guardrails | guard: rejected (broken_markup): tags opened or closed without their pair: <section> -> give_up | rejected broken_markup 2 |
| accessguru-053 | region | yes | fixed the target but broke another rule | verify: Your change made another rule fail, "landmark-unique": <aside aria-label="Advertisement 1"><span class="adv_txt">Advertisement</span></aside> (Fix any of the following: The landmark must have a unique aria-label, | no objections |
| accessguru-061 | region | yes | the target still fails | verify: Your change removed content (kept 88% of the words and 100% of the elements). Keep all existing text and elements. -> give_up | no objections |
| accessguru-065 | color-contrast | no | the target still fails | verify: The rule "color-contrast" still fails: <p class="hide-on-tablet">Despite brutally murdering and harming babies Lucy Letby has been heard moaning to...</p> (Fix any of the following: Element has insufficient color | no objections |
| accessguru-067 | color-contrast | no | fixed the target but broke another rule | verify: Your change made another rule fail, "color-contrast-enhanced": <a href="/signup" class="btn" data-trackingcategory="sign_up" data-trackinglabel="Sign up - Middle">Sign up now</a> (Fix any of the following: Elemen | no objections |
| accessguru-068 | color-contrast | no | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins ".material-icons { color: #a1a1a1; background-color: #00848f; }"). Copy it exactly from the HTML shown; the HTML may be a fragment, so t | no objections |
| accessguru-072 | color-contrast | no | the target still fails | verify: The rule "color-contrast" still fails: <a href="https://www.citizen.co.za/sport/cricket/" class="bg-category-cricket"> | no objections |
| accessguru-074 | color-contrast | no | fixed the target but broke another rule | verify: Your change made another rule fail, "color-contrast-enhanced": <span class="menu-title"> | no objections |
| accessguru-099 | link-name | no | edits did not match the page | propose: edits could not be applied: Edit 1: the find text was not found (it begins "<a dt-eid="banner_poster" dt-params="busModId=swiper&amp;mod_id=swiper&amp;vid=…"). Copy it exactly from the HTML shown; the HTML may b | no objections |
| accessguru-108 | link-name | no | edits did not match the page | propose: edits could not be applied: Edit 1: the find text occurs 4 times ("<a class="b-trending_carousel-slide_product_link" href="https://www.boohoo.com/…"). If every copy needs the same change, set replace_all to true | no objections |
| accessguru-116 | link-name | no | the target still fails | verify: The rule "link-name" still fails: <a data-tracking="{"teaser_lead_text_v..." href="/uk/scotland/article..." class="js-tracking"> (Fix all of the following: Element is in tab order and does not have accessible tex | no objections |
| accessguru-128 | landmark-one-main | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | no objections |
| accessguru-130 | landmark-one-main | yes | every attempt rejected by the guardrails | guard: rejected (broken_markup): a second <html> -> give_up | rejected broken_markup 2 |
| accessguru-132 | landmark-one-main | yes | every attempt rejected by the guardrails | guard: rejected (broken_markup): a second <html> -> give_up | rejected broken_markup 2 |
| accessguru-133 | landmark-one-main | yes | every attempt rejected by the guardrails | guard: rejected (broken_markup): a second <html> -> give_up | rejected broken_markup 2 |
| accessguru-136 | page-has-heading-one | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | no objections |
| accessguru-139 | page-has-heading-one | yes | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h1>Content Page</h1> (Fix any of the following: Some page content is not contained by landmarks). To pass "region": All page content is contained by landmarks. -> gi | rejected broken_markup 2 |
