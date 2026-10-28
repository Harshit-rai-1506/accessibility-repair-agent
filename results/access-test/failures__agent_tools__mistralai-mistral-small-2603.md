# Failures: agent_tools on access test (mistralai:mistral-small-2603)

Made with `python -m a11y_agent.evaluate failures --save`.

25 of 100 items not resolved safely by agent_tools.

| Why | Items |
|---|---|
| fixed the target but broke another rule | 8 |
| edits did not match the page | 7 |
| the target still fails | 6 |
| fixed for the scanner, but fails the guardrail checks | 4 |

| Item | Rule | Page-level | Why | Last step | Guardrails |
|---|---|---|---|---|---|
| access-007 | page-has-heading-one | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | - |
| access-026 | empty-heading | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <section><h3 class="uabb-info-list-title">Info List Title</h3></section> (Fix any of the following: Some page content is not contained by landmarks). To pass "region" | - |
| access-030 | landmark-one-main | yes | fixed for the scanner, but fails the guardrail checks | verify: passed -> accept | - |
| access-032 | region | yes | fixed for the scanner, but fails the guardrail checks | verify: passed -> accept | - |
| access-036 | link-name | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <a href="/" class="sc-1ekvrxa-5 hWYBpO" aria-label="Home"> | - |
| access-037 | empty-heading | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h4 aria-label="Heading"></h4> (Fix any of the following: Some page content is not contained by landmarks); <h5 class="sc-1yfyf2z-9 jKosFG" aria-label="Subheading"></ | - |
| access-038 | heading-order | no | the target still fails | verify: Your change removed content (kept 100% of the words and 56% of the elements). Keep all existing text and elements. This is the same change as attempt 2, which did not work either: try a different approach. -> giv | - |
| access-049 | aria-required-children | no | the target still fails | verify: Your change removed content (kept 100% of the words and 50% of the elements). Keep all existing text and elements. -> give_up | - |
| access-056 | region | yes | fixed the target but broke another rule | verify: Your change made another rule fail, "landmark-no-duplicate-contentinfo": <footer> (Fix any of the following: Document has more than one contentinfo landmark). To pass "landmark-no-duplicate-contentinfo": Document | - |
| access-060 | heading-order | no | the target still fails | verify: Your change removed content (kept 100% of the words and 50% of the elements). Keep all existing text and elements. This is the same change as attempt 1, which did not work either: try a different approach. -> giv | - |
| access-069 | page-has-heading-one | yes | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</html>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at  | - |
| access-071 | empty-heading | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h2 class="module__title">Module Title</h2> (Fix any of the following: Some page content is not contained by landmarks). To pass "region": All page content is contain | - |
| access-079 | region | yes | fixed the target but broke another rule | verify: Your change made another rule fail, "list": <ul> (Fix all of the following: List element has direct children that are not allowed: footer). -> give_up | - |
| access-082 | empty-heading | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <h2 class="h4">Section Heading</h2> (Fix any of the following: Some page content is not contained by landmarks); <h3>Subsection Heading</h3> (Fix any of the following | - |
| access-086 | landmark-one-main | yes | fixed for the scanner, but fails the guardrail checks | verify: passed -> accept | - |
| access-088 | aria-allowed-role | no | fixed the target but broke another rule | verify: Your change made another rule fail, "region": <img class="threePointInfo-graph-item-image" src="https://res.cloudinary.com/grubhub-assets/image/upload/v1567194984/illustration_1_gh_tqpnjw.svg" alt="local restaura | - |
| access-112 | link-name | no | edits did not match the page | propose: edits could not be applied: Edit 1: the find text was not found (it begins "<a class="css-4b75e" href="https://www.nytimes.com/interactive/2023/08/14/us/po…"). Copy it exactly from the HTML shown; the HTML may b | - |
| access-118 | nested-interactive | no | the target still fails | verify: The rule "nested-interactive" still fails: <div role="tab" class="css-jsjvin"> | - |
| access-128 | aria-allowed-attr | no | edits did not match the page | propose: edits could not be applied: Edit 2: the find text was not found (it begins "</div>"). Copy it exactly from the HTML shown; the HTML may be a fragment, so tags such as </body> may not exist. To add something at t | - |
| access-137 | region | yes | fixed for the scanner, but fails the guardrail checks | verify: passed -> accept | - |
