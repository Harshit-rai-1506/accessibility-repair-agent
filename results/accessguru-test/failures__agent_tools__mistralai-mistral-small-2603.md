# Failures: agent_tools on accessguru test (mistralai:mistral-small-2603)

Made with `python -m a11y_agent.evaluate failures --save`.

28 of 138 items not resolved by agent_tools.

| Why | Items |
|---|---|
| edits did not match the page | 10 |
| the target still fails | 9 |
| fixed the target but broke another rule | 9 |

| Item | Rule | Page-level | Why | Last step | Guardrails |
|---|---|---|---|---|---|
| accessguru-043 | region | yes | edits did not match the page | propose: edits could not be applied: Edit 5: the find text was not found (it begins "<span>Shop By Category</span> <a href="/collections/shop-all-categories" class=…"). Copy it exactly from the HTML shown; the HTML may b | - |
| accessguru-050 | region | yes | the target still fails | verify: The rule "region" still fails: <a id="psMainLogo" href="/" aria-label="Pluralsight" data-aa-title="PN-Pluralsight" tabindex="0"> | - |
| accessguru-052 | region | yes | fixed the target but broke another rule | verify: Your change made another rule fail, "landmark-main-is-top-level": <main><section><a class="LoginToaster__TitleWrapper-qps5ip-3 fFJDdQ">Unlock deals just for you!</a></section></main> (Fix any of the following: Th | - |
| accessguru-053 | region | yes | fixed the target but broke another rule | verify: Your change made another rule fail, "landmark-unique": <aside aria-label="Advertisement 1"><span class="adv_txt">Advertisement</span></aside> (Fix any of the following: The landmark must have a unique aria-label, | - |
| accessguru-061 | region | yes | the target still fails | verify: Your change removed content (kept 88% of the words and 100% of the elements). Keep all existing text and elements. -> give_up | - |
| accessguru-065 | color-contrast | no | the target still fails | verify: The rule "color-contrast" still fails: <p class="hide-on-tablet">Despite brutally murdering and harming babies Lucy Letby has been heard moaning to...</p> (Fix any of the following: Element has insufficient color | - |
| accessguru-067 | color-contrast | no | fixed the target but broke another rule | verify: Your change made another rule fail, "color-contrast-enhanced": <a href="/signup" class="btn" data-trackingcategory="sign_up" data-trackinglabel="Sign up - Middle">Sign up now</a> (Fix any of the following: Elemen | - |
| accessguru-068 | color-contrast | no | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins ".material-icons { color: #a1a1a1; background-color: #00848f; }"). Copy it exactly from the HTML shown; the HTML may be a fragment, so t | - |
| accessguru-072 | color-contrast | no | the target still fails | verify: The rule "color-contrast" still fails: <a href="https://www.citizen.co.za/sport/cricket/" class="bg-category-cricket"> | - |
| accessguru-074 | color-contrast | no | fixed the target but broke another rule | verify: Your change made another rule fail, "color-contrast-enhanced": <span class="menu-title"> | - |
| accessguru-099 | link-name | no | edits did not match the page | propose: edits could not be applied: Edit 1: the find text was not found (it begins "<a dt-eid="banner_poster" dt-params="busModId=swiper&amp;mod_id=swiper&amp;vid=…"). Copy it exactly from the HTML shown; the HTML may b | - |
| accessguru-108 | link-name | no | edits did not match the page | propose: edits could not be applied: Edit 1: the find text occurs 4 times ("<a class="b-trending_carousel-slide_product_link" href="https://www.boohoo.com/…"). If every copy needs the same change, set replace_all to true | - |
| accessguru-116 | link-name | no | the target still fails | verify: The rule "link-name" still fails: <a data-tracking="{"teaser_lead_text_v..." href="/uk/scotland/article..." class="js-tracking"> (Fix all of the following: Element is in tab order and does not have accessible tex | - |
| accessguru-128 | landmark-one-main | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | - |
| accessguru-136 | page-has-heading-one | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | - |
| accessguru-145 | page-has-heading-one | yes | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h1>Welcome</h1> (Fix any of the following: Some page content is not contained by landmarks). To pass "region": All page content is contained by landmarks. -> give_up | - |
| accessguru-146 | page-has-heading-one | yes | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h1>Inhalt</h1> (Fix any of the following: Some page content is not contained by landmarks). To pass "region": All page content is contained by landmarks. -> give_up | - |
| accessguru-148 | page-has-heading-one | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | - |
| accessguru-149 | page-has-heading-one | yes | the target still fails | verify: The rule "page-has-heading-one" still fails: <html style="height:100%"> (Fix all of the following: Page must have a level-one heading). -> give_up | - |
| accessguru-150 | page-has-heading-one | yes | edits did not match the page | propose: edits could not be applied: Edit 3: the find text was not found (it begins "<h1>Page Content</h1>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add | - |
