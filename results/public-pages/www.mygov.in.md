# https://www.mygov.in/

Checked 2026-10-28 with mistralai:mistral-small-2603, agent with tools and guardrails (`scripts/public_pages.py`).

- **Violation score: 30 -> 7** on the saved snapshot (the live page scored 42 with its stylesheets and images).
- Model calls: 19, about 1,659,825 tokens.
- Guardrails: input screen found nothing; rejected attempts: broken_markup 3.

| Rule | What happened | Attempts |
|---|---|---|
| button-name | fixed | 1 |
| select-name | fixed | 1 |
| frame-title | fixed | 3 |
| target-size | not fixed | 3 |
| heading-order | fixed | 1 |
| region | not fixed | 3 |
| aria-allowed-role | fixed | 2 |
| image-redundant-alt | fixed | 1 |
| presentation-role-conflict | fixed by earlier fix | 0 |

For a person to check before using the fix:

- adds text only screen readers hear, written without seeing the page: aria-label="Slide"; aria-label="Current Slide"; aria-label="Previous Slide"; aria-label="Next Slide"; title="Instagram Reel: MyGov India"; screen-reader-only text "a", "category", "select"
- marks 9 image(s) as decorative (alt=""): "/themes/custom/mygov_radix/src/assets/images/tiranga.jpg", "https://static.mygov.in/static/resources/s3fs-public/2025-01/quiz-mygov.png", "https://static.mygov.in/static/resources/s3fs-public/2025-01/transforming-india.png" and 6 more. Right only if they carry no information.

Still failing: target-size, region.

The first 40 lines of the change:

```diff
--- before
+++ after
@@ -152,3 +152,3 @@
 
-            <a class="goi external-link" href="https://www.india.gov.in/" title="GOVERNMENT OF INDIA" target="_blank" data-once="external-link-check external-link-click"><img src="/themes/custom/mygov_radix/src/assets/images/tiranga.jpg" data-align="left" alt="Government of India" width="33" height="22">GOVERNMENT OF INDIA</a>
+            <a class="goi external-link" href="https://www.india.gov.in/" title="GOVERNMENT OF INDIA" target="_blank" data-once="external-link-check external-link-click"><img src="/themes/custom/mygov_radix/src/assets/images/tiranga.jpg" data-align="left" alt="" width="33" height="22">GOVERNMENT OF INDIA</a>
       
@@ -314,3 +314,3 @@
   
-  <select data-drupal-selector="edit-category" id="edit-category" name="category" class="form-select form-control"><option value="all" selected="selected">All Categories</option><option value="task">Do/Tasks</option><option value="discuss">Discuss</option><option value="poll_survey">Poll/Survey</option><option value="blog_entry">Blog</option><option value="talk">Talk</option><option value="quiz">Quiz</option><option value="Prime_Json">Prime</option><option value="campaign">Campaigns</option><option value="pledge_json">Pledge</option><option value="podcast">Podcast</option><option value="media">Media</option><option value="mygov_ebook">Periodicals</option></select>
+  <label for="edit-category" class="sr-only">Select a category</label><select data-drupal-selector="edit-category" id="edit-category" name="category" class="form-select form-control"><option value="all" selected="selected">All Categories</option><option value="task">Do/Tasks</option><option value="discuss">Discuss</option><option value="poll_survey">Poll/Survey</option><option value="blog_entry">Blog</option><option value="talk">Talk</option><option value="quiz">Quiz</option><option value="Prime_Json">Prime</option><option value="campaign">Campaigns</option><option value="pledge_json">Pledge</option><option value="podcast">Podcast</option><option value="media">Media</option><option value="mygov_ebook">Periodicals</option></select>
 
@@ -750,3 +750,3 @@
           <img loading="lazy" src="https://static.mygov.in/static/resources/s3fs-public/2026-10/mygov_1792046573_4d2c83c3.jpg" alt="My Culture My Story Contest">
-        </a></div></div></div><div class="owl-nav"><button type="button" role="presentation" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" role="presentation" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots disabled"></div><a href="javascript:void(0)" class="play" title="Play Slider" style="display:none">Play</a><a href="javascript:void(0)" title="Pause Slider" class="stop">Pause</a></div>
+        </a></div></div></div><div class="owl-nav"><button type="button" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots disabled"></div><a href="javascript:void(0)" class="play" title="Play Slider" style="display:none">Play</a><a href="javascript:void(0)" title="Pause Slider" class="stop">Pause</a></div>
       </div>
@@ -1530,3 +1530,3 @@
     </div>
-  </div></div></div></div><div class="owl-nav disabled"><button type="button" role="presentation" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" role="presentation" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots"><button role="button" class="owl-dot"><span></span></button><button role="button" class="owl-dot"><span></span></button><button role="button" class="owl-dot active"><span></span></button></div></div></div>
+  </div></div></div></div><div class="owl-nav disabled"><button type="button" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots"><button role="button" class="owl-dot" aria-label="Slide"><span></span></button><button role="button" class="owl-dot" aria-label="Slide"><span></span></button><button role="button" class="owl-dot active" aria-label="Current Slide"><span></span></button></div></div></div>
 
@@ -1614,3 +1614,3 @@
     </div>
-  </div></div></div></div><div class="owl-nav disabled"><button type="button" role="presentation" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" role="presentation" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots"><button role="button" class="owl-dot"><span></span></button><button role="button" class="owl-dot"><span></span></button><button role="button" class="owl-dot active"><span></span></button></div></div></div>
+  </div></div></div></div><div class="owl-nav disabled"><button type="button" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots"><button role="button" class="owl-dot" aria-label="Slide"><span></span></button><button role="button" class="owl-dot" aria-label="Slide"><span></span></button><button role="button" class="owl-dot active" aria-label="Current Slide"><span></span></button></div></div></div>
 
@@ -2297,3 +2297,3 @@
           </div>
-        </div></div></div></div><div class="owl-nav"><button type="button" role="presentation" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" role="presentation" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots disabled"></div></div>
+        </div></div></div></div><div class="owl-nav"><button type="button" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots disabled"></div></div>
     </div>
@@ -2850,3 +2850,3 @@
         </div>
-      </div></div></div></div><div class="owl-nav"><button type="button" role="presentation" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" role="presentation" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots disabled"></div></div>
+      </div></div></div></div><div class="owl-nav"><button type="button" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots disabled"></div></div>
     <div class="center-align"> <a href="https://quiz.mygov.in/" class="more-btn" target="_blank" data-once="external-link-check">View All</a></div>
@@ -2953,3 +2953,3 @@
         19, 2026 </span> </div>
-  </div></div></div></div><div class="owl-nav disabled"><button type="button" role="presentation" class="owl-prev"><span aria-label="Previous">‹</span></button><button type="button" role="presentation" class="owl-next"><span aria-label="Next">›</span></button></div><div class="owl-dots disabled"></div></div>
... 97 more diff lines
```
