# Failures: agent_guarded on access test (mistralai:mistral-small-2603)

Made with `python -m a11y_agent.evaluate failures --save`.

25 of 100 items not resolved safely by agent_guarded.

| Why | Items |
|---|---|
| edits did not match the page | 9 |
| fixed the target but broke another rule | 6 |
| every attempt rejected by the guardrails | 5 |
| the target still fails | 5 |

| Item | Rule | Page-level | Why | Last step | Guardrails |
|---|---|---|---|---|---|
| access-007 | page-has-heading-one | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | no objections |
| access-026 | empty-heading | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <section><h3 class="uabb-info-list-title">Info List Title</h3></section> (Fix any of the following: Some page content is not contained by landmarks). To pass "region" | no objections |
| access-030 | landmark-one-main | yes | every attempt rejected by the guardrails | guard: rejected (broken_markup): a second <html> -> give_up | rejected broken_markup 2 |
| access-032 | region | yes | every attempt rejected by the guardrails | guard: rejected (broken_markup): tags opened or closed without their pair: <section> -> give_up | rejected broken_markup 2; flagged adds_landmarks 1 |
| access-036 | link-name | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <a href="/" class="sc-1ekvrxa-5 hWYBpO" aria-label="Home"> | flagged added_text_alternative 1 |
| access-037 | empty-heading | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h4 aria-label="Heading"></h4> (Fix any of the following: Some page content is not contained by landmarks); <h5 class="sc-1yfyf2z-9 jKosFG" aria-label="Subheading"></ | no objections |
| access-038 | heading-order | no | the target still fails | verify: Your change removed content (kept 100% of the words and 56% of the elements). Keep all existing text and elements. This is the same change as attempt 2, which did not work either: try a different approach. -> giv | no objections |
| access-049 | aria-required-children | no | the target still fails | verify: Your change removed content (kept 100% of the words and 50% of the elements). Keep all existing text and elements. -> give_up | no objections |
| access-056 | region | yes | every attempt rejected by the guardrails | guard: rejected (removes_content): elements <a> -> give_up | rejected removes_content 1 |
| access-060 | heading-order | no | the target still fails | verify: Your change removed content (kept 100% of the words and 50% of the elements). Keep all existing text and elements. This is the same change as attempt 1, which did not work either: try a different approach. -> giv | no objections |
| access-069 | page-has-heading-one | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | no objections |
| access-071 | empty-heading | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h2 class="module__title">Module Title</h2> (Fix any of the following: Some page content is not contained by landmarks). To pass "region": All page content is contain | no objections |
| access-079 | region | yes | every attempt rejected by the guardrails | guard: rejected (broken_markup): tags opened or closed without their pair: <div> -> give_up | rejected broken_markup 1 |
| access-082 | empty-heading | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h2 class="h4">Section Heading</h2> (Fix any of the following: Some page content is not contained by landmarks); <h3>Subsection Heading</h3> (Fix any of the following | no objections |
| access-086 | landmark-one-main | yes | every attempt rejected by the guardrails | guard: rejected (broken_markup): a second <html> -> give_up | rejected broken_markup 2 |
| access-088 | aria-allowed-role | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <img class="threePointInfo-graph-item-image" src="https://res.cloudinary.com/grubhub-assets/image/upload/v1567194984/illustration_1_gh_tqpnjw.svg" alt="local restaura | no objections |
| access-112 | link-name | no | edits did not match the page | propose: edits could not be applied: Edit 1: the find text was not found (it begins "<a class="css-4b75e" href="https://www.nytimes.com/interactive/2023/08/14/us/po…"). Copy it exactly from the HTML shown; the HTML may b | no objections |
| access-118 | nested-interactive | no | the target still fails | verify: The rule "nested-interactive" still fails: <div role="tab" class="css-jsjvin"> | no objections |
| access-128 | aria-allowed-attr | no | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</div>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at t | no objections |
| access-137 | region | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text occurs 4 times ("<div class="responsive-feature-module bg-white rm-p-0 full">"). If every copy needs the same change, set replace_all to true; otherwise include  | rejected broken_markup 1 |
